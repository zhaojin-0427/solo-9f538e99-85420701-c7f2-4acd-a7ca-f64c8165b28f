"""Interleaving tests: queries during recalculation, concurrent corrections."""

import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.test_api import sample_game


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BASKETBALL_DB", str(tmp_path / "test.db"))
    db.init_db()
    app.state.before_commit_hook = None
    with TestClient(app) as c:
        yield c
    app.state.before_commit_hook = None


def test_query_during_recalculation_sees_previous_version(client):
    """While a correction batch is being recomputed (after validation, before
    commit), concurrent queries still get the previous version's stats."""
    assert client.post("/games", json=sample_game("g1")).status_code == 201

    recompute_started = threading.Event()
    allow_commit = threading.Event()

    def hook(game_id, new_version):
        recompute_started.set()
        assert allow_commit.wait(timeout=10)

    app.state.before_commit_hook = hook

    result = {}

    def run_correction():
        result["resp"] = client.post("/games/g1/corrections", json={"corrections": [
            {"seq": 6, "expected_revision": 0,
             "event": {"type": "score", "team": "B", "player": "B6", "points": 3}},
        ]})

    t = threading.Thread(target=run_correction)
    t.start()
    try:
        assert recompute_started.wait(timeout=5), "correction never reached recompute phase"
        # recalculation in progress -> previous version still served
        mid = client.get("/games/g1/stats")
        assert mid.status_code == 200
        assert mid.json()["version"] == 1
        assert mid.json()["score"] == {"A": 5, "B": 5}
        # events still at old revisions too
        assert all(e["revision"] == 0 for e in client.get("/games/g1/events").json()["events"])
    finally:
        allow_commit.set()
        t.join(timeout=10)

    assert result["resp"].status_code == 200, result["resp"].text
    assert result["resp"].json()["version"] == 2
    # after commit the new version is live
    after = client.get("/games/g1/stats").json()
    assert after["version"] == 2
    assert after["score"] == {"A": 5, "B": 6}


def test_concurrent_corrections_on_distinct_events(client):
    """Two recorders correct different events at the same time. Both batches
    reference untouched revisions, so both succeed (serialized); the second
    one is validated against the record including the first one's changes."""
    assert client.post("/games", json=sample_game("g1")).status_code == 201

    barrier = threading.Barrier(2)
    results = {}

    def correct(name, payload):
        barrier.wait(timeout=10)
        results[name] = client.post("/games/g1/corrections", json=payload)

    batch1 = {"corrections": [
        {"seq": 1, "expected_revision": 0,
         "event": {"type": "score", "team": "A", "player": "A2", "points": 2}}]}
    batch2 = {"corrections": [
        {"seq": 6, "expected_revision": 0,
         "event": {"type": "score", "team": "B", "player": "B6", "points": 3}}]}

    t1 = threading.Thread(target=correct, args=("b1", batch1))
    t2 = threading.Thread(target=correct, args=("b2", batch2))
    t1.start(); t2.start(); t1.join(timeout=15); t2.join(timeout=15)

    assert results["b1"].status_code == 200, results["b1"].text
    assert results["b2"].status_code == 200, results["b2"].text
    versions = sorted(r.json()["version"] for r in results.values())
    assert versions == [2, 3]  # serialized: one applied first, the other on top

    final = client.get("/games/g1/stats").json()
    assert final["version"] == 3
    assert final["score"] == {"A": 5, "B": 6}
    pm = final["plus_minus"]
    assert pm["A"]["A2"] == -1   # +2 (seq1, now A2) -3 (seq3) +3 (seq4) -3 (seq6)
    assert pm["A"]["A1"] == -1
    assert pm["B"]["B6"] == 3
    events = client.get("/games/g1/events").json()["events"]
    assert [e["revision"] for e in events] == [1, 0, 0, 0, 0, 1]


def test_concurrent_corrections_on_same_event_one_wins(client):
    """Two recorders correct the SAME event at the same time: exactly one
    batch wins; the loser gets a 409 revision conflict and stats stay
    consistent with the winner's version."""
    assert client.post("/games", json=sample_game("g1")).status_code == 201

    barrier = threading.Barrier(2)
    results = {}

    def correct(name, points):
        barrier.wait(timeout=10)
        results[name] = client.post("/games/g1/corrections", json={"corrections": [
            {"seq": 6, "expected_revision": 0,
             "event": {"type": "score", "team": "B", "player": "B6", "points": points}}]})

    t1 = threading.Thread(target=correct, args=("w", 3))
    t2 = threading.Thread(target=correct, args=("l", 1))
    t1.start(); t2.start(); t1.join(timeout=15); t2.join(timeout=15)

    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409]
    conflict = next(r for r in results.values() if r.status_code == 409)
    assert conflict.json()["detail"]["conflicts"] == [
        {"seq": 6, "expected_revision": 0, "current_revision": 1}]

    final = client.get("/games/g1/stats").json()
    assert final["version"] == 2  # exactly one batch applied
    winner_points = next(p for r, p in ((results["w"], 3), (results["l"], 1)) if r.status_code == 200)
    assert final["score"] == {"A": 5, "B": 3 + winner_points}
