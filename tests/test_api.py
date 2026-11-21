import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BASKETBALL_DB", str(tmp_path / "test.db"))
    db.init_db()
    app.state.before_commit_hook = None
    with TestClient(app) as c:
        yield c
    app.state.before_commit_hook = None


def sample_game(game_id="g1"):
    return {
        "game_id": game_id,
        "team_a": {"name": "Tigers",
                   "players": ["A1", "A2", "A3", "A4", "A5", "A6", "A7"],
                   "starters": ["A1", "A2", "A3", "A4", "A5"]},
        "team_b": {"name": "Sharks",
                   "players": ["B1", "B2", "B3", "B4", "B5", "B6", "B7"],
                   "starters": ["B1", "B2", "B3", "B4", "B5"]},
        "events": [
            {"type": "score", "team": "A", "player": "A1", "points": 2},    # seq 1
            {"type": "substitution", "team": "A", "out": "A5", "in": "A6"},  # seq 2
            {"type": "score", "team": "B", "player": "B1", "points": 3},    # seq 3
            {"type": "score", "team": "A", "player": "A6", "points": 3},    # seq 4
            {"type": "substitution", "team": "B", "out": "B5", "in": "B6"},  # seq 5
            {"type": "score", "team": "B", "player": "B6", "points": 2},    # seq 6
        ],
    }


def import_ok(client, game_id="g1"):
    r = client.post("/games", json=sample_game(game_id))
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ import

def test_import_and_plus_minus_math(client):
    body = import_ok(client)
    assert body["version"] == 1
    stats = body["stats"]
    assert stats["score"] == {"A": 5, "B": 5}
    pm = stats["plus_minus"]
    # A5 left after seq1 (+2); A6 played seq3-4 (-3+3) and seq6 (-2)
    assert pm["A"] == {"A1": 0, "A2": 0, "A3": 0, "A4": 0, "A5": 2, "A6": -2, "A7": 0}
    # B5 left before seq6 (-2+3-3); B6 only played seq6 (+2)
    assert pm["B"] == {"B1": 0, "B2": 0, "B3": 0, "B4": 0, "B5": -2, "B6": 2, "B7": 0}
    assert stats["on_court"] == {"A": ["A1", "A2", "A3", "A4", "A6"],
                                 "B": ["B1", "B2", "B3", "B4", "B6"]}


def test_import_rejects_invalid_record_and_stores_nothing(client):
    game = sample_game()
    game["events"][3] = {"type": "score", "team": "A", "player": "A5", "points": 3}  # A5 subbed out at seq2
    r = client.post("/games", json=game)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "validation_failed"
    assert [e["seq"] for e in detail["errors"]] == [4]
    assert "not on court" in detail["errors"][0]["message"]
    assert client.get("/games/g1").status_code == 404


def test_import_rejects_bad_substitution(client):
    game = sample_game()
    game["events"][1] = {"type": "substitution", "team": "A", "out": "A6", "in": "A7"}  # A6 not on court
    r = client.post("/games", json=game)
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["seq"] == 2


def test_import_duplicate_game_id_conflicts(client):
    import_ok(client)
    r = client.post("/games", json=sample_game("g1"))
    assert r.status_code == 409


def test_events_listing_shows_revisions(client):
    import_ok(client)
    r = client.get("/games/g1/events")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert [e["seq"] for e in body["events"]] == [1, 2, 3, 4, 5, 6]
    assert all(e["revision"] == 0 for e in body["events"])
    assert body["events"][1] == {"seq": 2, "revision": 0, "type": "substitution",
                                 "team": "A", "out": "A5", "in": "A6"}


# -------------------------------------------------------------- corrections

def test_correction_success_bumps_version_and_recomputes(client):
    import_ok(client)
    r = client.post("/games/g1/corrections", json={"corrections": [
        {"seq": 6, "expected_revision": 0,
         "event": {"type": "score", "team": "B", "player": "B6", "points": 3}},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["applied"] == [{"seq": 6, "from_revision": 0, "to_revision": 1}]
    assert body["stats"]["score"] == {"A": 5, "B": 6}
    assert body["stats"]["plus_minus"]["B"]["B6"] == 3
    assert body["stats"]["plus_minus"]["A"]["A6"] == -3

    # events now show revision 1 for seq 6, others untouched
    events = client.get("/games/g1/events").json()["events"]
    assert [e["revision"] for e in events] == [0, 0, 0, 0, 0, 1]
    # current stats endpoint serves the new version
    assert client.get("/games/g1/stats").json()["version"] == 2


def test_revision_conflict_points_out_events_and_changes_nothing(client):
    import_ok(client)
    client.post("/games/g1/corrections", json={"corrections": [
        {"seq": 6, "expected_revision": 0,
         "event": {"type": "score", "team": "B", "player": "B6", "points": 3}},
    ]})
    # recorder resubmits against the stale revision 0
    r = client.post("/games/g1/corrections", json={"corrections": [
        {"seq": 6, "expected_revision": 0,
         "event": {"type": "score", "team": "B", "player": "B6", "points": 2}},
        {"seq": 99, "expected_revision": 0,
         "event": {"type": "score", "team": "B", "player": "B6", "points": 2}},
    ]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["conflicts"] == [
        {"seq": 6, "expected_revision": 0, "current_revision": 1},
        {"seq": 99, "expected_revision": 0, "current_revision": None, "message": "event not found"},
    ]
    # stats and events unchanged
    assert client.get("/games/g1/stats").json()["version"] == 2
    events = client.get("/games/g1/events").json()["events"]
    assert events[5]["revision"] == 1 and events[5]["points"] == 3


def test_correction_validated_against_latest_effective_record(client):
    """Other events already corrected: a batch whose own revisions still match
    is validated against the latest effective record, not the original one."""
    import_ok(client)
    # batch 1: A7 enters instead of A6, and it is A7 who scores at seq 4
    r = client.post("/games/g1/corrections", json={"corrections": [
        {"seq": 2, "expected_revision": 0,
         "event": {"type": "substitution", "team": "A", "out": "A5", "in": "A7"}},
        {"seq": 4, "expected_revision": 0,
         "event": {"type": "score", "team": "A", "player": "A7", "points": 3}},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    # batch 2: touches only seq 6 (still at its original revision) -> must be
    # validated against the record that already includes batch 1
    r = client.post("/games/g1/corrections", json={"corrections": [
        {"seq": 6, "expected_revision": 0,
         "event": {"type": "score", "team": "B", "player": "B6", "points": 3}},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 3
    assert body["stats"]["score"] == {"A": 5, "B": 6}
    pm = body["stats"]["plus_minus"]
    assert pm["A"]["A7"] == -3   # -3 (seq3) +3 (seq4) -3 (seq6, now 3pts); on court seq3..6
    assert pm["A"]["A6"] == 0    # never played in the corrected record
    assert pm["B"]["B6"] == 3


def test_correction_validation_failure_points_out_events(client):
    import_ok(client)
    # pretend A5 (subbed out at seq 2) scored at seq 4 -> invalid
    r = client.post("/games/g1/corrections", json={"corrections": [
        {"seq": 4, "expected_revision": 0,
         "event": {"type": "score", "team": "A", "player": "A5", "points": 3}},
    ]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "validation_failed"
    assert [e["seq"] for e in detail["errors"]] == [4]
    # original events and stats untouched
    assert client.get("/games/g1/stats").json()["version"] == 1
    assert all(e["revision"] == 0 for e in client.get("/games/g1/events").json()["events"])


def test_correction_batch_is_atomic(client):
    import_ok(client)
    # first item valid, second item stale revision -> whole batch rejected
    r = client.post("/games/g1/corrections", json={"corrections": [
        {"seq": 1, "expected_revision": 0,
         "event": {"type": "score", "team": "A", "player": "A2", "points": 2}},
        {"seq": 6, "expected_revision": 5,
         "event": {"type": "score", "team": "B", "player": "B6", "points": 2}},
    ]})
    assert r.status_code == 409
    assert client.get("/games/g1/stats").json()["version"] == 1


def test_duplicate_seq_in_one_batch_rejected(client):
    import_ok(client)
    r = client.post("/games/g1/corrections", json={"corrections": [
        {"seq": 1, "expected_revision": 0,
         "event": {"type": "score", "team": "A", "player": "A1", "points": 2}},
        {"seq": 1, "expected_revision": 0,
         "event": {"type": "score", "team": "A", "player": "A2", "points": 2}},
    ]})
    assert r.status_code == 400
    assert r.json()["detail"]["seqs"] == [1]


def test_previous_version_stats_remain_queryable(client):
    import_ok(client)
    before = client.get("/games/g1/stats").json()
    client.post("/games/g1/corrections", json={"corrections": [
        {"seq": 6, "expected_revision": 0,
         "event": {"type": "score", "team": "B", "player": "B6", "points": 3}},
    ]})
    old = client.get("/games/g1/stats", params={"version": 1}).json()
    assert old == before
    assert client.get("/games/g1/stats", params={"version": 99}).status_code == 404
    versions = client.get("/games/g1/versions").json()["versions"]
    assert [(v["version"], v["kind"]) for v in versions] == [(1, "import"), (2, "correction")]
    assert versions[1]["detail"]["corrections"] == [{"seq": 6, "from_revision": 0, "to_revision": 1}]


def test_unknown_game_404(client):
    assert client.get("/games/nope/stats").status_code == 404
    assert client.get("/games/nope/events").status_code == 404
    r = client.post("/games/nope/corrections", json={"corrections": [
        {"seq": 1, "expected_revision": 0,
         "event": {"type": "score", "team": "A", "player": "A1", "points": 2}}]})
    assert r.status_code == 404
