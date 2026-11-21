"""Pure game-record simulation: validates a full game record and computes stats.

No I/O here — the API layer loads the effective record, hands it to
simulate(), and only persists the result when validation passes.
"""

ALLOWED_POINTS = {1, 2, 3}
TEAMS = ("A", "B")


def simulate(rosters, starters, events):
    """Validate a whole game record and compute score / plus-minus.

    rosters:  {"A": [player_id, ...], "B": [...]}
    starters: {"A": [5 player_ids],   "B": [...]}
    events:   [{"seq": int, "type": "substitution"|"score", "payload": {...}}]
              in ascending seq order.

    Returns (stats, errors). stats is None when errors is non-empty.
    Every error identifies the event it belongs to via "seq" (None for
    roster/starter problems, which are not tied to a single event).
    """
    errors = []

    for team in TEAMS:
        roster = rosters.get(team, [])
        five = starters.get(team, [])
        if len(roster) != len(set(roster)):
            errors.append({"seq": None, "message": f"team {team}: duplicate players in roster"})
        if len(roster) < 5:
            errors.append({"seq": None, "message": f"team {team}: roster needs at least 5 players, got {len(roster)}"})
        if len(five) != 5:
            errors.append({"seq": None, "message": f"team {team}: exactly 5 starters required, got {len(five)}"})
        if len(set(five)) != len(five):
            errors.append({"seq": None, "message": f"team {team}: duplicate starters"})
        for p in five:
            if p not in roster:
                errors.append({"seq": None, "message": f"team {team}: starter {p!r} is not in the roster"})
    if errors:
        return None, errors

    on_court = {"A": list(starters["A"]), "B": list(starters["B"])}
    plus_minus = {"A": {p: 0 for p in rosters["A"]}, "B": {p: 0 for p in rosters["B"]}}
    score = {"A": 0, "B": 0}

    for ev in events:
        seq, etype, pl = ev["seq"], ev["type"], ev.get("payload") or {}

        if etype == "substitution":
            team = pl.get("team")
            out, inn = pl.get("out"), pl.get("in")
            if team not in TEAMS:
                errors.append({"seq": seq, "message": f"invalid team {team!r}"})
                continue
            if out not in on_court[team]:
                errors.append({"seq": seq, "message": f"team {team}: player {out!r} to be substituted out is not on court"})
                continue
            if inn not in rosters[team]:
                errors.append({"seq": seq, "message": f"team {team}: substitute {inn!r} is not in the roster"})
                continue
            if inn in on_court[team]:
                errors.append({"seq": seq, "message": f"team {team}: substitute {inn!r} is already on court"})
                continue
            on_court[team] = [inn if p == out else p for p in on_court[team]]

        elif etype == "score":
            team = pl.get("team")
            player, points = pl.get("player"), pl.get("points")
            if team not in TEAMS:
                errors.append({"seq": seq, "message": f"invalid team {team!r}"})
                continue
            if not isinstance(points, int) or isinstance(points, bool) or points not in ALLOWED_POINTS:
                errors.append({"seq": seq, "message": f"invalid points {points!r} (must be 1, 2 or 3)"})
                continue
            if player not in rosters[team]:
                errors.append({"seq": seq, "message": f"team {team}: scorer {player!r} is not in the roster"})
                continue
            if player not in on_court[team]:
                errors.append({"seq": seq, "message": f"team {team}: scorer {player!r} is not on court"})
                continue
            other = "B" if team == "A" else "A"
            score[team] += points
            for p in on_court[team]:
                plus_minus[team][p] += points
            for p in on_court[other]:
                plus_minus[other][p] -= points

        else:
            errors.append({"seq": seq, "message": f"unknown event type {etype!r}"})

    if errors:
        return None, errors
    stats = {"score": score, "plus_minus": plus_minus, "on_court": on_court}
    return stats, []
