"""Season simulation (D11, CONTRACT.md 6.5).

Monte Carlo over the UNPLAYED regular-season games with every team's Elo frozen at its
current value. Each remaining game is drawn once per run from

    p_home = win_prob(elo_home - elo_away + hfa * [not neutral] + rest_per_day * rest_diff)

with no quarterback term (future starters are unknown). The per-run outcome is added to the
real record, standings are formed per conference (4 division winners, then 3 wild cards),
seeded, and an approximate playoff bracket is played (2v7, 3v6, 4v5; reseed; divisional;
conference final with home field to the higher seed; neutral Super Bowl).

Approximations, stated on the page via the returned ``notes`` / ``sim_notes`` copy:
  * TIES ARE NOT SIMULATED. Every simulated game has a winner. Real ties already on a team's
    record are kept (they count as half a win when ranking standings) but no new tie is drawn.
  * Standings ties are broken by record (2*w + t), then frozen Elo plus a tiny random jitter,
    NOT the NFL's head-to-head / common-games / strength-of-victory procedure.
  * Playoff games use team Elo + HFA for the higher seed, no rest or QB term; Super Bowl neutral.

Only regular-season rows are simulated; anything else passed in ``remaining_games`` (playoff
rows, already-played rows) is ignored.

Everything here is pure: ``random.Random(seed)`` is the only randomness, inputs are never
mutated, and all numbers are emitted at full precision (round for display only).

Performance: the hot loop works on small lists of ints/floats indexed by team position
(0..31) and never copies a dict per run; 10,000 runs take well under a second here.
"""

from __future__ import annotations

import math
import random

DEFAULT_RUNS = 10000
DEFAULT_SEED = 2026
JITTER = 1e-6  # Elo jitter scale for standings tie-breaks (far below any real Elo gap)
PLAYOFF_TEAMS_PER_CONFERENCE = 7
DIVISIONS_PER_CONFERENCE = 4


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


def win_prob(d: float) -> float:
    """Elo logistic: probability the side with rating edge ``d`` wins (1/(1+10**(-d/400)))."""
    return 1.0 / (1.0 + 10.0 ** (-d / 400.0))


def game_probability(
    elo_home: float,
    elo_away: float,
    neutral: bool,
    home_rest: int | None,
    away_rest: int | None,
    hfa: float,
    rest_per_day: float,
) -> float:
    """Pregame home win probability for a simulated game: team Elo + HFA + rest, no QB term.

    The rest term is 0 when EITHER rest value is missing (D1 ground truth).
    """
    rest_elo = 0.0
    if home_rest is not None and away_rest is not None:
        rest_elo = rest_per_day * (home_rest - away_rest)
    hfa_elo = 0.0 if neutral else hfa
    return win_prob(elo_home - elo_away + hfa_elo + rest_elo)


def _team_code(team: dict) -> str:
    """A team entry's canonical code: ``code`` (teams.json) or ``team`` (model.json teams[])."""
    code = team.get("code") or team.get("team")
    if not code:
        raise ValueError("team entry has neither 'code' nor 'team': %r" % (team,))
    return code


def prepare(
    teams: list[dict],
    remaining_games: list[dict],
    ratings: dict,
    params: dict,
    records: dict | None = None,
) -> dict:
    """Build the compact, index-based state the simulation loop runs on.

    ``teams``: 32 team metadata dicts (conference, division) optionally carrying ``record``
    {w,l,t}; ``records`` (code -> {w,l,t}) overrides/supplies records when given.
    ``ratings``: code -> frozen Elo. ``params``: the params object (uses params["elo"]["hfa"]
    and ["rest_per_day"]). Unplayed REG rows only are kept from ``remaining_games``.

    Returns a dict of plain lists (nothing here is mutated by the loop).
    """
    elo_params = params["elo"]
    hfa = float(elo_params["hfa"])
    rest_per_day = float(elo_params["rest_per_day"])

    ordered = sorted(teams, key=_team_code)
    codes = [_team_code(t) for t in ordered]
    if len(codes) != len(set(codes)):
        raise ValueError("duplicate team codes in teams")
    index = {code: i for i, code in enumerate(codes)}

    conferences = sorted({t["conference"] for t in ordered})
    division_names = sorted({t["division"] for t in ordered})
    conf_index = {c: i for i, c in enumerate(conferences)}
    div_index = {d: i for i, d in enumerate(division_names)}
    conf_of = [conf_index[t["conference"]] for t in ordered]
    div_of = [div_index[t["division"]] for t in ordered]
    div_conf = {}
    for t in ordered:
        div_conf[div_index[t["division"]]] = conf_index[t["conference"]]
    # teams per division / divisions per conference, as index lists (fixed for the whole sim)
    div_members = [[i for i in range(len(codes)) if div_of[i] == d] for d in range(len(division_names))]
    conf_divisions = [[d for d in range(len(division_names)) if div_conf[d] == c] for c in range(len(conferences))]
    conf_members = [[i for i in range(len(codes)) if conf_of[i] == c] for c in range(len(conferences))]

    elo = []
    for code in codes:
        if code not in ratings:
            raise ValueError("no rating for team %s" % code)
        elo.append(float(ratings[code]))

    base_w, base_t, base_pts = [], [], []
    for t in ordered:
        code = _team_code(t)
        rec = (records or {}).get(code) if records is not None else None
        if rec is None:
            rec = t.get("record")
        if rec is None:
            raise ValueError("no record for team %s" % code)
        w, tie = int(rec.get("w", 0)), int(rec.get("t", 0))
        base_w.append(w)
        base_t.append(tie)
        base_pts.append(2 * w + tie)  # standings points: a tie is half a win

    games = []
    skipped = 0
    for g in remaining_games:
        if g.get("game_type", "REG") != "REG" or g.get("played", False):
            skipped += 1
            continue
        home, away = g["home"], g["away"]
        if home not in index or away not in index:
            raise ValueError("remaining game %s has an unknown team" % g.get("game_id"))
        p = game_probability(
            elo[index[home]],
            elo[index[away]],
            bool(g.get("neutral", False)),
            g.get("home_rest"),
            g.get("away_rest"),
            hfa,
            rest_per_day,
        )
        games.append((index[home], index[away], p))

    return {
        "codes": codes,
        "elo": elo,
        "hfa": hfa,
        "conf_members": conf_members,
        "conf_divisions": conf_divisions,
        "div_members": div_members,
        "base_w": base_w,
        "base_t": base_t,
        "base_pts": base_pts,
        "games": games,
        "skipped_rows": skipped,
    }


def _play(rng_random, elo_a: float, elo_b: float, edge_a: float) -> bool:
    """True when side A wins a single playoff game; ``edge_a`` is A's non-rating Elo edge (HFA)."""
    return rng_random() < win_prob(elo_a - elo_b + edge_a)


def run_once(state: dict, rng: random.Random) -> dict:
    """One full simulated season on the prepared ``state``. Debug/testing hook.

    Returns {"wins": [sim wins per team index], "division_winners": [team idx per division],
    "seeds": [[7 team idx per conference, seed 1 first], ...], "conference_champions": [...],
    "super_bowl": team idx}.
    """
    rnd = rng.random
    elo = state["elo"]
    hfa = state["hfa"]
    pts = list(state["base_pts"])
    wins = [0] * len(pts)

    for h, a, p in state["games"]:
        if rnd() < p:
            pts[h] += 2
            wins[h] += 1
        else:
            pts[a] += 2
            wins[a] += 1

    # standings key: record first, then Elo + jitter (bounded below 0.001 Elo points)
    key = [pts[i] * 1e6 + elo[i] + rnd() * JITTER for i in range(len(pts))]

    division_winners = [max(members, key=key.__getitem__) for members in state["div_members"]]
    seeds = []
    champions = []
    for c, divisions in enumerate(state["conf_divisions"]):
        winners = sorted((division_winners[d] for d in divisions), key=key.__getitem__, reverse=True)
        winner_set = set(winners)
        others = [i for i in state["conf_members"][c] if i not in winner_set]
        others.sort(key=key.__getitem__, reverse=True)
        s = winners + others[: PLAYOFF_TEAMS_PER_CONFERENCE - len(winners)]
        seeds.append(s)
        champions.append(_conference_playoff(s, elo, hfa, rnd))

    a, b = champions[0], champions[1]
    sb = a if _play(rnd, elo[a], elo[b], 0.0) else b
    return {
        "wins": wins,
        "division_winners": division_winners,
        "seeds": seeds,
        "conference_champions": champions,
        "super_bowl": sb,
    }


def _conference_playoff(seeds: list[int], elo: list[float], hfa: float, rnd) -> int:
    """Play one conference bracket. ``seeds`` is 7 team indexes, seed 1 first.

    Wild card: 2v7, 3v6, 4v5 (1 rests); reseed so the best remaining seed hosts the worst;
    conference final hosted by the higher seed. Returns the champion's team index.
    """
    def host_wins(hi: int, lo: int) -> int:
        # hi = higher seed (hosts). Returns the winner's index.
        return seeds[hi] if _play(rnd, elo[seeds[hi]], elo[seeds[lo]], hfa) else seeds[lo]

    # positions in `seeds` (0 = seed 1 ... 6 = seed 7)
    alive = [0]
    for hi, lo in ((1, 6), (2, 5), (3, 4)):
        w = host_wins(hi, lo)
        alive.append(seeds.index(w))
    alive.sort()  # reseed: lowest position = best seed
    # divisional: best vs worst, middle pair
    d1 = host_wins(alive[0], alive[3])
    d2 = host_wins(alive[1], alive[2])
    p1, p2 = seeds.index(d1), seeds.index(d2)
    if p1 > p2:
        p1, p2 = p2, p1
    return host_wins(p1, p2)


# --------------------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------------------


def simulate_counts(
    teams: list[dict],
    remaining_games: list[dict],
    ratings: dict,
    params: dict,
    runs: int = DEFAULT_RUNS,
    seed: int = DEFAULT_SEED,
    records: dict | None = None,
) -> dict:
    """Run the simulation and return RAW INTEGER COUNTS per team (exact-sum testing hook).

    {"runs", "seed", "counts": {code: {"wins": int (summed sim wins), "playoffs", "division",
    "seed1", "super_bowl"}}, "state": prepared state}
    """
    if runs < 1:
        raise ValueError("runs must be >= 1")
    state = prepare(teams, remaining_games, ratings, params, records)
    rng = random.Random(seed)
    n = len(state["codes"])
    c_wins = [0] * n
    c_playoffs = [0] * n
    c_div = [0] * n
    c_seed1 = [0] * n
    c_sb = [0] * n
    for _ in range(runs):
        r = run_once(state, rng)
        w = r["wins"]
        for i in range(n):
            c_wins[i] += w[i]
        for i in r["division_winners"]:
            c_div[i] += 1
        for s in r["seeds"]:
            c_seed1[s[0]] += 1
            for i in s:
                c_playoffs[i] += 1
        c_sb[r["super_bowl"]] += 1
    counts = {}
    for i, code in enumerate(state["codes"]):
        counts[code] = {
            "wins": c_wins[i],
            "playoffs": c_playoffs[i],
            "division": c_div[i],
            "seed1": c_seed1[i],
            "super_bowl": c_sb[i],
        }
    return {"runs": runs, "seed": seed, "counts": counts, "state": state}


def sim_notes(runs: int, seed: int) -> list[str]:
    """Page copy describing the simulation's approximations (CONTRACT 6.5 ``notes``)."""
    mc = 100.0 * 0.5 / math.sqrt(runs)  # worst-case (p=0.5) standard error, in points
    return [
        "Each remaining regular-season game is drawn from the team's frozen Elo plus home field "
        "and rest; quarterback adjustments are not applied to future games.",
        "Ties are not simulated: every simulated game has a winner.",
        "Standings ties are broken by record, then Elo plus a tiny random jitter, not the NFL's "
        "head-to-head and common-games rules.",
        "Playoffs: division winners seeded 1-4 by record and three wild cards 5-7; 2v7, 3v6, 4v5, "
        "reseeded divisional round, conference final with home field to the higher seed, neutral "
        "Super Bowl. Playoff games use team Elo and home field only.",
        "%s runs, seed %d; probabilities are counts divided by runs, so the Monte Carlo error is "
        "up to about %.1f percentage points." % (format(runs, ","), seed, mc),
    ]


def simulate(
    teams: list[dict],
    remaining_games: list[dict],
    ratings: dict,
    params: dict,
    runs: int = DEFAULT_RUNS,
    seed: int = DEFAULT_SEED,
    records: dict | None = None,
) -> dict:
    """CONTRACT 6.5: simulate the rest of the regular season + approximate playoffs.

    teams            teams.json entries (or model.json teams[]) each carrying ``record`` {w,l,t},
                     unless ``records`` (code -> {w,l,t}, e.g. replay["records"]) is given.
    remaining_games  normalized game dicts (CONTRACT section 1); only unplayed REG rows are used.
    ratings          replay["ratings"]: code -> frozen Elo. Never mutated.
    params           params object; reads params["elo"]["hfa"] and ["rest_per_day"].

    Returns {"runs", "seed", "teams": {code: {"projected_wins", "playoffs", "division", "seed1",
    "super_bowl"}}, "notes": [str], "sim_notes": [str]} — ``projected_wins`` = real wins + mean
    simulated wins (existing ties are not counted as wins); the four probabilities are
    counts / runs at full precision. ``sim_notes`` duplicates ``notes`` under the page-copy key.
    """
    raw = simulate_counts(teams, remaining_games, ratings, params, runs, seed, records)
    state = raw["state"]
    out = {}
    for i, code in enumerate(state["codes"]):
        c = raw["counts"][code]
        out[code] = {
            "projected_wins": state["base_w"][i] + c["wins"] / runs,
            "playoffs": c["playoffs"] / runs,
            "division": c["division"] / runs,
            "seed1": c["seed1"] / runs,
            "super_bowl": c["super_bowl"] / runs,
        }
    notes = sim_notes(runs, seed)
    return {"runs": runs, "seed": seed, "teams": out, "notes": notes, "sim_notes": list(notes)}


def attach(team_entries: list[dict], sim: dict) -> list[dict]:
    """Return copies of model.json ``teams[]`` entries with ``simulation`` (CONTRACT 2.10) filled.

    Convenience for build.py: ``{"runs", "projected_wins", "playoffs", "division", "seed1",
    "super_bowl"}`` per team. Input entries are not mutated.
    """
    result = []
    for t in team_entries:
        code = _team_code(t)
        s = sim["teams"][code]
        entry = dict(t)
        entry["simulation"] = {
            "runs": sim["runs"],
            "projected_wins": s["projected_wins"],
            "playoffs": s["playoffs"],
            "division": s["division"],
            "seed1": s["seed1"],
            "super_bowl": s["super_bowl"],
        }
        result.append(entry)
    return result
