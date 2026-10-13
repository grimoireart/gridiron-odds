"""Schema constants and model.json validation (docs/CONTRACT.md is normative).

Everything here is pure: constants derived from src/teams.json at import time
and `validate_model`, which returns a list of problem strings (empty = valid).
No file is written and nothing is mutated.
"""
from __future__ import annotations

import json
import os
import re

SCHEMA_VERSION = "1.0.0"
OVERLAY_SCHEMA_VERSION = "1.0.0"
LEDGER_SCHEMA_VERSION = "1.0.0"

NFLVERSE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
)
LEAGUE_TZ = "America/New_York"
DEFAULT_KICKOFF_ET = "13:00"  # used when gametime is blank (time_known=false)

# Confidence tiers (D4): names used everywhere, bounds on the favorite's p_blend.
TIER_TOSSUP = "Toss-up"
TIER_LEAN = "Lean"
TIER_STRONG = "Strong"
TIER_LOCKISH = "Lock-ish"
TIERS = (TIER_TOSSUP, TIER_LEAN, TIER_STRONG, TIER_LOCKISH)
TIER_BOUNDS = {"lean": 0.55, "strong": 0.65, "lockish": 0.78}

DEFAULT_BOOKS = ("draftkings", "fanduel", "betmgm", "betrivers")  # D16 (2026-10-09: Caesars absent from feed)
BOOK_TITLES = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "betrivers": "BetRivers",
    "williamhill_us": "Caesars",
}
CSV_BOOK_KEY = "csv"  # pseudo-book used when the official market comes from CSV lines

GAME_TYPES = ("REG", "WC", "DIV", "CON", "SB")
GAME_STATUSES = ("scheduled", "final")
MARKET_SOURCES = ("odds_api", "csv")
TOTAL_SOURCES = ("odds_api", "csv", "league_average")
EDGE_REASON_KEYS = ("starter_change", "injuries", "rest_gap", "form_move", "unknown")  # D17 + D18.4 priority order
RATIONALE_KEYS = ("rating", "home_field", "quarterback", "rest", "injuries", "form", "market", "verdict")
LEDGER_EVENT_TYPES = ("snapshot", "grade")
EDGE_THRESHOLD = 0.08  # D17
EV_POSITIVE_THRESHOLD = 0.02  # D8
PARLAY_MIN_P = 0.55  # D9
OVERLAY_MATCH_WINDOW_HOURS = 36  # D6
DISPLAY_TOLERANCE = 1.5  # D5 display ints: 0.5 rounding + the 1-point favorite nudge on a tie
# Rationale triggers (CONTRACT 2.6): a key is present iff its trigger fires, in RATIONALE_KEYS order.
RATIONALE_QB_GAP = 30.0  # |qb_elo| >= this (or a starter change / first start) -> "quarterback"
RATIONALE_REST_GAP = 3  # |rest_diff| >= this days -> "rest"
RATIONALE_FORM_MOVE = 40.0  # |form_*_3| >= this Elo points -> "form"
RATIONALE_MARKET_GAP = 0.05  # |model_market_gap_pick| >= this -> "market"
# Injury reports (D18, CONTRACT 2.6b): "injuries" iff a starter is Out or Doubtful on either side.
INJURY_STATUSES = ("Out", "Doubtful", "Questionable", "Probable")  # severity order; Probable only before 2016
INJURY_GROUPS = ("OL", "WR_TE", "RB", "DL_EDGE", "LB", "DB", "QB", "ST")
INJURY_WEIGHT_GROUPS = ("OL", "WR_TE", "RB", "DL_EDGE", "LB", "DB")  # params.injury.weights keys (QB/ST carry no weight)

# CSV team-code aliases (nflverse history + other feeds) -> canonical code.
_TEAMS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "teams.json")


def load_teams(path: str = _TEAMS_PATH) -> list[dict]:
    """Return the 32 team metadata dicts from src/teams.json (sorted by code)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    teams = sorted(data["teams"], key=lambda t: t["code"])
    if len(teams) != 32:
        raise ValueError("teams.json must list exactly 32 teams, found %d" % len(teams))
    return teams


TEAM_META = load_teams()
TEAMS = tuple(t["code"] for t in TEAM_META)  # 32 canonical codes, sorted
TEAM_SET = frozenset(TEAMS)
CONFERENCES = ("AFC", "NFC")
DIVISIONS = {}
for _t in TEAM_META:
    DIVISIONS.setdefault(_t["division"], []).append(_t["code"])
DIVISION_OF = {t["code"]: t["division"] for t in TEAM_META}
CONFERENCE_OF = {t["code"]: t["conference"] for t in TEAM_META}

# code alias -> canonical code (identity entries included so ALIAS.get(x, x) is never needed)
ALIAS = {t["code"]: t["code"] for t in TEAM_META}
for _t in TEAM_META:
    for _a in _t.get("code_aliases", []):
        ALIAS[_a] = _t["code"]

# lowercased full name (and every alias) -> canonical code (for the odds feed)
NAME_TO_CODE = {}
for _t in TEAM_META:
    NAME_TO_CODE[_t["full_name"].lower()] = _t["code"]
    for _a in _t.get("aliases", []):
        NAME_TO_CODE[_a.lower()] = _t["code"]
CODE_TO_FULL_NAME = {t["code"]: t["full_name"] for t in TEAM_META}


def canonical_code(code: str | None) -> str | None:
    """Map an nflverse/feed team code to the canonical code; None if unknown."""
    if not code:
        return None
    return ALIAS.get(code.strip().upper())


def code_from_name(name: str | None) -> str | None:
    """Map an odds-feed full team name (any alias) to the canonical code; None if unknown."""
    if not name:
        return None
    return NAME_TO_CODE.get(" ".join(name.split()).lower())


def tier_for(p_fav: float, bounds: dict | None = None) -> str:
    """Tier name for the favorite's blended probability (D4)."""
    b = bounds or TIER_BOUNDS
    if p_fav >= b["lockish"]:
        return TIER_LOCKISH
    if p_fav >= b["strong"]:
        return TIER_STRONG
    if p_fav >= b["lean"]:
        return TIER_LEAN
    return TIER_TOSSUP


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------

_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GAME_ID = re.compile(r"^\d{4}_\d{2}_[A-Z]{2,3}_[A-Z]{2,3}$")

OFFICIAL_KEYS = frozenset(
    [
        "pick",
        "p_home_model",
        "p_home_market",
        "p_home_blend",
        "p_pick_blend",
        "tier",
        "model_spread",
        "blend_spread",
        "expected_home",
        "expected_away",
        "display_home",
        "display_away",
        "total_used",
        "total_source",
        "market",
        "best_prices",
        "best_price",
        "estimated_ev_per_dollar",
        "factors",
        "injuries",
        "rationale",
        "rationale_sentences",
        "edge",
        "parlay_eligible",
    ]
)
INJURIES_KEYS = frozenset(["enabled", "as_of", "home", "away", "elo_home", "elo_away", "elo"])
INJURY_REPORT_KEYS = frozenset(
    [
        "available",
        "report_filed",
        "players",
        "starters_out",
        "starters_doubtful",
        "starters_questionable",
        "total_listed",
        "qb_listed",
    ]
)
INJURY_PLAYER_KEYS = frozenset(
    ["gsis_id", "name", "position", "pos_group", "status", "injury", "starter", "covered_by_qb_term"]
)
PARAMS_INJURY_KEYS = frozenset(["enabled", "weights", "status_mult", "gate"])
PARAMS_INJURY_GATE_KEYS = frozenset(["holdout_brier_with", "holdout_brier_without", "improvement_required"])
GAME_KEYS = frozenset(
    [
        "game_id",
        "season",
        "game_type",
        "week",
        "league_date",
        "kickoff_utc",
        "time_known",
        "away",
        "home",
        "neutral",
        "status",
        "availability",
        "official",
        "result",
        "frozen_snapshot",
    ]
)
AVAILABILITY_KEYS = frozenset(["starter_home", "starter_away", "market", "csv_lines"])
MARKET_KEYS = frozenset(
    [
        "source",
        "as_of",
        "consensus_p_home",
        "consensus_total",
        "consensus_home_spread",
        "n_books",
        "books",
    ]
)
BOOK_KEYS = frozenset(["key", "home_ml", "away_ml", "home_spread", "total"])
PRICE_KEYS = frozenset(["book", "side", "american"])
RESULT_KEYS = frozenset(
    [
        "home_score",
        "away_score",
        "winner",
        "margin",
        "hit",
        "pred_margin_error",
        "pred_total_error",
    ]
)
EDGE_KEYS = frozenset(["flag", "reason_key", "reason_text"])
FACTOR_KEYS = frozenset(
    [
        "team_elo_home",
        "team_elo_away",
        "elo_gap_home",
        "home_rank",
        "away_rank",
        "hfa_elo",
        "hfa_spread_pts",
        "rest_home",
        "rest_away",
        "rest_diff",
        "rest_elo",
        "qb_home",
        "qb_away",
        "qb_elo",
        "form_home_3",
        "form_away_3",
        "d_total",
        "model_market_gap_home",
        "model_market_gap_pick",
    ]
)
QB_FACTOR_KEYS = frozenset(
    [
        "id",
        "name",
        "q",
        "starts",
        "first_start",
        "usual_starter_id",
        "usual_starter_name",
        "usual_starter_q",
        "differs_from_usual",
        "source",
    ]
)
TEAM_KEYS = frozenset(
    [
        "team",
        "name",
        "city",
        "conference",
        "division",
        "full_name",
        "aliases",
        "elo",
        "rank",
        "record",
        "trend",
        "qb",
        "simulation",
    ]
)
SIM_KEYS = frozenset(["runs", "projected_wins", "playoffs", "division", "seed1", "super_bowl"])
TOP_KEYS = frozenset(
    ["schema_version", "build", "params", "evaluation", "games", "teams", "scorecard", "track_record"]
)
TRACK_RECORD_KEYS = frozenset(
    ["overall", "by_tier", "by_week", "by_injury_load", "calibration", "score_error", "clv", "units"]
)
INJURY_LOADS = ("starter_out", "no_starter_out", "unknown")  # D18.5 by_injury_load buckets, in order
BUILD_KEYS = frozenset(["id", "built_at", "official_odds_at", "league_date", "next_league_date", "odds_source", "data", "injuries"])
BUILD_INJURIES_KEYS = frozenset(["available", "injuries_rows", "depth_chart_as_of", "teams_with_report_for_next_slate"])


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_prob(x) -> bool:
    return _is_num(x) and 0.0 <= x <= 1.0


def _check_keys(obj: dict, expected: frozenset, where: str, problems: list[str]) -> bool:
    """Require obj to be a dict with exactly the expected keys."""
    if not isinstance(obj, dict):
        problems.append("%s: expected object, got %s" % (where, type(obj).__name__))
        return False
    missing = sorted(expected - set(obj))
    extra = sorted(set(obj) - expected)
    if missing:
        problems.append("%s: missing keys %s" % (where, missing))
    if extra:
        problems.append("%s: unexpected keys %s" % (where, extra))
    return not missing and not extra


def _check_price(price, where: str, problems: list[str], allowed_books=None) -> None:
    if price is None:
        return
    if not _check_keys(price, PRICE_KEYS, where, problems):
        return
    if not isinstance(price["book"], str) or not price["book"]:
        problems.append("%s.book: must be a non-empty string" % where)
    elif allowed_books is not None and price["book"] not in allowed_books:
        problems.append("%s.book: %r not in %s" % (where, price["book"], sorted(allowed_books)))
    if price["side"] not in TEAM_SET:
        problems.append("%s.side: %r is not a canonical team" % (where, price["side"]))
    if not _is_int(price["american"]) or abs(price["american"]) < 100:
        problems.append("%s.american: must be an int with |x| >= 100" % where)


def _check_qb(qb, where: str, problems: list[str]) -> None:
    if qb is None:
        return
    if not _check_keys(qb, QB_FACTOR_KEYS, where, problems):
        return
    if not isinstance(qb["id"], str) or not qb["id"]:
        problems.append("%s.id: must be a non-empty string" % where)
    if not isinstance(qb["name"], str) or not qb["name"]:
        problems.append("%s.name: must be a non-empty string" % where)
    if not _is_num(qb["q"]):
        problems.append("%s.q: must be a number" % where)
    if not _is_int(qb["starts"]) or qb["starts"] < 0:
        problems.append("%s.starts: must be a non-negative int" % where)
    if not isinstance(qb["first_start"], bool):
        problems.append("%s.first_start: must be bool" % where)
    if qb["first_start"] != (qb["starts"] == 0):
        problems.append("%s.first_start must equal (starts == 0)" % where)
    if qb["usual_starter_id"] is not None and not isinstance(qb["usual_starter_id"], str):
        problems.append("%s.usual_starter_id: string or null" % where)
    if qb["usual_starter_q"] is not None and not _is_num(qb["usual_starter_q"]):
        problems.append("%s.usual_starter_q: number or null" % where)
    if not isinstance(qb["differs_from_usual"], bool):
        problems.append("%s.differs_from_usual: must be bool" % where)
    if qb["source"] != "nflverse_listed":
        problems.append("%s.source: must be 'nflverse_listed'" % where)


def _check_injury_report(rep, where: str, problems: list[str]) -> None:
    """CONTRACT 2.6b report: exact keys, player shape, ordering, counts, the unavailable zeros."""
    if not _check_keys(rep, INJURY_REPORT_KEYS, where, problems):
        return
    for k in ("available", "report_filed"):
        if not isinstance(rep[k], bool):
            problems.append("%s.%s: must be bool" % (where, k))
    for k in ("starters_out", "starters_doubtful", "starters_questionable", "total_listed"):
        if not _is_int(rep[k]) or rep[k] < 0:
            problems.append("%s.%s: non-negative int" % (where, k))
    if rep["qb_listed"] is not None and rep["qb_listed"] not in INJURY_STATUSES:
        problems.append("%s.qb_listed: one of %s or null" % (where, INJURY_STATUSES))
    if not isinstance(rep["players"], list):
        problems.append("%s.players: must be a list" % where)
        return
    ok = True
    for i, p in enumerate(rep["players"]):
        pw = "%s.players[%d]" % (where, i)
        if not _check_keys(p, INJURY_PLAYER_KEYS, pw, problems):
            ok = False
            continue
        if p["gsis_id"] is not None and (not isinstance(p["gsis_id"], str) or not p["gsis_id"]):
            problems.append("%s.gsis_id: non-empty string or null" % pw)
        if not isinstance(p["name"], str) or not p["name"]:
            problems.append("%s.name: non-empty string" % pw)
        for k in ("position", "injury"):
            if p[k] is not None and not isinstance(p[k], str):
                problems.append("%s.%s: string or null" % (pw, k))
        if p["pos_group"] not in INJURY_GROUPS:
            problems.append("%s.pos_group: %r not in %s" % (pw, p["pos_group"], INJURY_GROUPS))
            ok = False
        if p["status"] not in INJURY_STATUSES:
            problems.append("%s.status: %r not in %s" % (pw, p["status"], INJURY_STATUSES))
            ok = False
        for k in ("starter", "covered_by_qb_term"):
            if not isinstance(p[k], bool):
                problems.append("%s.%s: must be bool" % (pw, k))
                ok = False
        if ok and p["covered_by_qb_term"] != (p["pos_group"] == "QB"):
            problems.append("%s.covered_by_qb_term must equal (pos_group == 'QB')" % pw)
    if not ok:
        return
    players = rep["players"]
    if rep["available"] is False:
        if rep["report_filed"] or players or rep["qb_listed"] is not None or any(
            rep[k] != 0 for k in ("starters_out", "starters_doubtful", "starters_questionable", "total_listed")
        ):
            problems.append("%s: unavailable report must be all false/[]/0/null" % where)
        return
    if players and not rep["report_filed"]:
        problems.append("%s.report_filed: must be true when players are listed" % where)
    if rep["total_listed"] != len(players):
        problems.append("%s.total_listed must equal len(players)" % where)
    counted = [p for p in players if p["starter"] and not p["covered_by_qb_term"]]
    for k, st in (("starters_out", "Out"), ("starters_doubtful", "Doubtful"), ("starters_questionable", "Questionable")):
        n = sum(1 for p in counted if p["status"] == st)
        if rep[k] != n:
            problems.append("%s.%s: %r but the players imply %d" % (where, k, rep[k], n))
    order = [(0 if p["starter"] else 1, INJURY_STATUSES.index(p["status"])) for p in players]
    if order != sorted(order):
        problems.append("%s.players: must list starters first, then Out, Doubtful, Questionable, Probable" % where)


def _injury_term(rep, params_injury) -> float:
    """The D18.6 formula (mirrors injuries.injury_elo) for the consistency check below."""
    if not params_injury.get("enabled") or not rep.get("available"):
        return 0.0
    w, m = params_injury.get("weights") or {}, params_injury.get("status_mult") or {}
    total = 0.0
    for p in rep.get("players") or []:
        if p.get("starter") and not p.get("covered_by_qb_term"):
            total += float(w.get(p.get("pos_group"), 0.0)) * float(m.get(p.get("status"), 0.0))
    return -total if total else 0.0


def _check_injuries(inj, where: str, problems: list[str], params_injury) -> None:
    """CONTRACT 2.6b official.injuries: shape, enabled mirrors params, elo arithmetic and zeros."""
    if not _check_keys(inj, INJURIES_KEYS, where, problems):
        return
    if not isinstance(inj["enabled"], bool):
        problems.append("%s.enabled: must be bool" % where)
    elif isinstance(params_injury, dict) and isinstance(params_injury.get("enabled"), bool) and inj["enabled"] != params_injury["enabled"]:
        problems.append("%s.enabled: must equal params.injury.enabled" % where)
    if not isinstance(inj["as_of"], str) or not _ISO_UTC.match(inj["as_of"]):
        problems.append("%s.as_of: must be ISO-8601 UTC 'YYYY-MM-DDTHH:MM:SSZ'" % where)
    for k in ("elo_home", "elo_away", "elo"):
        if not _is_num(inj[k]):
            problems.append("%s.%s: number" % (where, k))
    before = len(problems)
    _check_injury_report(inj["home"], where + ".home", problems)
    _check_injury_report(inj["away"], where + ".away", problems)
    if len(problems) != before or not all(_is_num(inj[k]) for k in ("elo_home", "elo_away", "elo")):
        return
    for k in ("elo_home", "elo_away"):
        if inj[k] > 1e-9:
            problems.append("%s.%s: must be <= 0 (a penalty)" % (where, k))
    if abs(inj["elo_home"] - inj["elo_away"] - inj["elo"]) > 1e-9:
        problems.append("%s.elo must equal elo_home - elo_away" % where)
    if inj["enabled"] is False and (inj["elo_home"] != 0 or inj["elo_away"] != 0):
        problems.append("%s: elo_home/elo_away must be 0 while the term is disabled" % where)
    for k, side in (("elo_home", "home"), ("elo_away", "away")):
        if inj[side]["available"] is False and inj[k] != 0:
            problems.append("%s.%s: must be 0 when the %s report is unavailable" % (where, k, side))
    if isinstance(params_injury, dict) and isinstance(params_injury.get("weights"), dict) and isinstance(params_injury.get("status_mult"), dict):
        for k, side in (("elo_home", "home"), ("elo_away", "away")):
            want = _injury_term(inj[side], params_injury)
            if abs(inj[k] - want) > 1e-6:
                problems.append("%s.%s: %r but D18.6 on the %s report gives %r" % (where, k, inj[k], side, want))


def _check_params_injury(pi, problems: list[str]) -> None:
    """params.injury (CONTRACT 2.3, D18): exact keys, ranges, and the enable gate."""
    where = "params.injury"
    if not _check_keys(pi, PARAMS_INJURY_KEYS, where, problems):
        return
    if not isinstance(pi["enabled"], bool):
        problems.append("%s.enabled: must be bool" % where)
    if _check_keys(pi["weights"], frozenset(INJURY_WEIGHT_GROUPS), where + ".weights", problems):
        for k, v in pi["weights"].items():
            if not _is_num(v) or v < 0:
                problems.append("%s.weights.%s: number >= 0" % (where, k))
    if _check_keys(pi["status_mult"], frozenset(INJURY_STATUSES), where + ".status_mult", problems):
        for k, v in pi["status_mult"].items():
            if not _is_num(v) or not 0.0 <= v <= 1.0:
                problems.append("%s.status_mult.%s: number in [0,1]" % (where, k))
    if _check_keys(pi["gate"], PARAMS_INJURY_GATE_KEYS, where + ".gate", problems):
        g = pi["gate"]
        for k in ("holdout_brier_with", "holdout_brier_without"):
            if g[k] is not None and (not _is_num(g[k]) or g[k] < 0):
                problems.append("%s.gate.%s: number >= 0 or null" % (where, k))
        if not _is_num(g["improvement_required"]) or g["improvement_required"] < 0:
            problems.append("%s.gate.improvement_required: number >= 0" % where)
        elif pi["enabled"] is True:
            w, wo = g["holdout_brier_with"], g["holdout_brier_without"]
            if not (_is_num(w) and _is_num(wo)) or (wo - w) < g["improvement_required"] - 1e-12:
                problems.append("%s.enabled: true but the gate is not met (holdout_brier_without - holdout_brier_with must be >= improvement_required)" % where)


def _check_market(market, where: str, problems: list[str]) -> None:
    if not _check_keys(market, MARKET_KEYS, where, problems):
        return
    if market["source"] not in MARKET_SOURCES:
        problems.append("%s.source: %r not in %s" % (where, market["source"], MARKET_SOURCES))
    if not isinstance(market["as_of"], str) or not _ISO_UTC.match(market["as_of"]):
        problems.append("%s.as_of: must be ISO-8601 UTC 'YYYY-MM-DDTHH:MM:SSZ'" % where)
    if market["consensus_p_home"] is not None and not _is_prob(market["consensus_p_home"]):
        problems.append("%s.consensus_p_home: probability or null" % where)
    for k in ("consensus_total", "consensus_home_spread"):
        if market[k] is not None and not _is_num(market[k]):
            problems.append("%s.%s: number or null" % (where, k))
    if not _is_int(market["n_books"]) or market["n_books"] < 0:
        problems.append("%s.n_books: non-negative int" % where)
    if not isinstance(market["books"], list):
        problems.append("%s.books: must be a list" % where)
        return
    if market["n_books"] != len(market["books"]):
        problems.append("%s.n_books must equal len(books)" % where)
    seen = set()
    for i, b in enumerate(market["books"]):
        bw = "%s.books[%d]" % (where, i)
        if not _check_keys(b, BOOK_KEYS, bw, problems):
            continue
        if not isinstance(b["key"], str) or not b["key"]:
            problems.append("%s.key: non-empty string" % bw)
        elif b["key"] in seen:
            problems.append("%s.key: duplicate book %r" % (bw, b["key"]))
        seen.add(b["key"])
        for k in ("home_ml", "away_ml"):
            if b[k] is not None and (not _is_int(b[k]) or abs(b[k]) < 100):
                problems.append("%s.%s: int with |x| >= 100, or null" % (bw, k))
        for k in ("home_spread", "total"):
            if b[k] is not None and not _is_num(b[k]):
                problems.append("%s.%s: number or null" % (bw, k))


def _check_game(g, i: int, problems: list[str], tier_bounds: dict, default_books, params_injury=None) -> None:
    where = "games[%d]" % i
    if not _check_keys(g, GAME_KEYS, where, problems):
        return
    gid = g["game_id"]
    where = "games[%d](%s)" % (i, gid)
    if not isinstance(gid, str) or not _GAME_ID.match(gid):
        problems.append("%s.game_id: bad format" % where)
    if not _is_int(g["season"]) or g["season"] < 1999:
        problems.append("%s.season: int >= 1999" % where)
    if g["game_type"] not in GAME_TYPES:
        problems.append("%s.game_type: %r not in %s" % (where, g["game_type"], GAME_TYPES))
    if not _is_int(g["week"]) or g["week"] < 1:
        problems.append("%s.week: positive int" % where)
    if not isinstance(g["league_date"], str) or not _ISO_DATE.match(g["league_date"]):
        problems.append("%s.league_date: 'YYYY-MM-DD'" % where)
    if not isinstance(g["kickoff_utc"], str) or not _ISO_UTC.match(g["kickoff_utc"]):
        problems.append("%s.kickoff_utc: ISO-8601 UTC 'YYYY-MM-DDTHH:MM:SSZ'" % where)
    for k in ("time_known", "neutral"):
        if not isinstance(g[k], bool):
            problems.append("%s.%s: must be bool" % (where, k))
    for k in ("home", "away"):
        if g[k] not in TEAM_SET:
            problems.append("%s.%s: %r is not a canonical team" % (where, k, g[k]))
    if g["home"] == g["away"]:
        problems.append("%s: home and away are the same team" % where)
    if g["status"] not in GAME_STATUSES:
        problems.append("%s.status: %r not in %s" % (where, g["status"], GAME_STATUSES))

    av = g["availability"]
    if _check_keys(av, AVAILABILITY_KEYS, where + ".availability", problems):
        for k in AVAILABILITY_KEYS:
            if not isinstance(av[k], bool):
                problems.append("%s.availability.%s: must be bool" % (where, k))

    o = g["official"]
    ow = where + ".official"
    if not _check_keys(o, OFFICIAL_KEYS, ow, problems):
        return
    if o["pick"] not in (g["home"], g["away"]):
        problems.append("%s.pick: must be home or away team" % ow)
    for k in ("p_home_model", "p_home_blend", "p_pick_blend"):
        if not _is_prob(o[k]):
            problems.append("%s.%s: probability in [0,1]" % (ow, k))
    if o["p_home_market"] is not None and not _is_prob(o["p_home_market"]):
        problems.append("%s.p_home_market: probability or null" % ow)
    if o["tier"] not in TIERS:
        problems.append("%s.tier: %r not in %s" % (ow, o["tier"], TIERS))
    for k in ("model_spread", "blend_spread", "expected_home", "expected_away", "total_used"):
        if not _is_num(o[k]):
            problems.append("%s.%s: number" % (ow, k))
    for k in ("display_home", "display_away"):
        if not _is_int(o[k]) or o[k] < 0:
            problems.append("%s.%s: non-negative int" % (ow, k))
    if o["total_source"] not in TOTAL_SOURCES:
        problems.append("%s.total_source: %r not in %s" % (ow, o["total_source"], TOTAL_SOURCES))
    if o["estimated_ev_per_dollar"] is not None and not _is_num(o["estimated_ev_per_dollar"]):
        problems.append("%s.estimated_ev_per_dollar: number or null" % ow)
    if not isinstance(o["parlay_eligible"], bool):
        problems.append("%s.parlay_eligible: must be bool" % ow)

    # consistency: pick is the blend favorite; p_pick_blend matches; tier matches
    if _is_prob(o["p_home_blend"]) and o["pick"] in (g["home"], g["away"]):
        fav_is_home = o["p_home_blend"] >= 0.5
        if (o["pick"] == g["home"]) != fav_is_home:
            problems.append("%s.pick: does not match p_home_blend favorite" % ow)
        p_pick = o["p_home_blend"] if fav_is_home else 1.0 - o["p_home_blend"]
        if _is_prob(o["p_pick_blend"]) and abs(o["p_pick_blend"] - p_pick) > 1e-9:
            problems.append("%s.p_pick_blend: must equal favorite's p_home_blend side" % ow)
        if o["tier"] in TIERS and tier_for(p_pick, tier_bounds) != o["tier"]:
            problems.append("%s.tier: %r but p_pick_blend implies %r" % (ow, o["tier"], tier_for(p_pick, tier_bounds)))
    if all(_is_num(o[k]) for k in ("expected_home", "expected_away", "total_used", "blend_spread")):
        if abs(o["expected_home"] + o["expected_away"] - o["total_used"]) > 1e-6:
            problems.append("%s: expected_home + expected_away must equal total_used" % ow)
        if abs(o["expected_home"] - o["expected_away"] - o["blend_spread"]) > 1e-6:
            problems.append("%s: expected_home - expected_away must equal blend_spread" % ow)
        if _is_prob(o.get("p_home_blend")) and (o["blend_spread"] > 0) != (o["p_home_blend"] > 0.5) and abs(o["blend_spread"]) > 1e-9:
            problems.append("%s: blend_spread sign must follow p_home_blend" % ow)
    if _is_int(o["display_home"]) and _is_int(o["display_away"]):
        # D5: round half away, then +1 to the favorite on a tie -> at most 0.5 + 1 = 1.5 from expected.
        if _is_num(o["expected_home"]) and abs(o["display_home"] - o["expected_home"]) > DISPLAY_TOLERANCE + 1e-9:
            problems.append("%s.display_home: more than %.1f points from expected_home" % (ow, DISPLAY_TOLERANCE))
        if _is_num(o["expected_away"]) and abs(o["display_away"] - o["expected_away"]) > DISPLAY_TOLERANCE + 1e-9:
            problems.append("%s.display_away: more than %.1f points from expected_away" % (ow, DISPLAY_TOLERANCE))

    # market / availability consistency
    if o["market"] is None:
        if av.get("market") is not False:
            problems.append("%s: market is null but availability.market is not false" % ow)
        if o["p_home_market"] is not None:
            problems.append("%s.p_home_market: must be null when market is null" % ow)
        if _is_prob(o["p_home_model"]) and _is_prob(o["p_home_blend"]) and abs(o["p_home_model"] - o["p_home_blend"]) > 1e-9:
            problems.append("%s: no market, so p_home_blend must equal p_home_model" % ow)
        if o["best_price"] is not None:
            problems.append("%s.best_price: must be null when market is null" % ow)
        if o["parlay_eligible"]:
            problems.append("%s.parlay_eligible: must be false when market is null" % ow)
    else:
        _check_market(o["market"], ow + ".market", problems)
        if av.get("market") is not True:
            problems.append("%s: market present but availability.market is not true" % ow)

    if not isinstance(o["best_prices"], dict):
        problems.append("%s.best_prices: must be an object keyed by book" % ow)
    else:
        if set(o["best_prices"]) != set(default_books):
            problems.append("%s.best_prices: keys must be exactly the default books %s" % (ow, sorted(default_books)))
        for bk, price in o["best_prices"].items():
            _check_price(price, "%s.best_prices[%s]" % (ow, bk), problems)
            if price is not None and price["book"] != bk:
                problems.append("%s.best_prices[%s].book must equal its key" % (ow, bk))
            if price is not None and price["side"] != o["pick"]:
                problems.append("%s.best_prices[%s].side must be the pick" % (ow, bk))
    _check_price(o["best_price"], ow + ".best_price", problems)
    if o["best_price"] is not None and o["best_price"]["side"] != o["pick"]:
        problems.append("%s.best_price.side must be the pick" % ow)
    if o["best_price"] is None and o["estimated_ev_per_dollar"] is not None:
        problems.append("%s.estimated_ev_per_dollar: must be null without best_price" % ow)

    # factors
    f = o["factors"]
    fw = ow + ".factors"
    if _check_keys(f, FACTOR_KEYS, fw, problems):
        for k in ("team_elo_home", "team_elo_away", "elo_gap_home", "hfa_elo", "hfa_spread_pts", "rest_elo", "qb_elo", "d_total"):
            if not _is_num(f[k]):
                problems.append("%s.%s: number" % (fw, k))
        for k in ("home_rank", "away_rank"):
            if not _is_int(f[k]) or not 1 <= f[k] <= 32:
                problems.append("%s.%s: int in 1..32" % (fw, k))
        for k in ("rest_home", "rest_away", "rest_diff"):
            if f[k] is not None and not _is_int(f[k]):
                problems.append("%s.%s: int or null" % (fw, k))
        for k in ("form_home_3", "form_away_3", "model_market_gap_home", "model_market_gap_pick"):
            if f[k] is not None and not _is_num(f[k]):
                problems.append("%s.%s: number or null" % (fw, k))
        if g["neutral"] is True and f["hfa_elo"] != 0:
            problems.append("%s.hfa_elo: must be 0 at a neutral site" % fw)
        if _is_num(f["team_elo_home"]) and _is_num(f["team_elo_away"]) and _is_num(f["elo_gap_home"]):
            if abs(f["team_elo_home"] - f["team_elo_away"] - f["elo_gap_home"]) > 1e-6:
                problems.append("%s.elo_gap_home must equal team_elo_home - team_elo_away" % fw)
        _check_qb(f["qb_home"], fw + ".qb_home", problems)
        _check_qb(f["qb_away"], fw + ".qb_away", problems)
        if (f["qb_home"] is None) != (av.get("starter_home") is False):
            problems.append("%s.qb_home null-ness must mirror availability.starter_home" % fw)
        if (f["qb_away"] is None) != (av.get("starter_away") is False):
            problems.append("%s.qb_away null-ness must mirror availability.starter_away" % fw)
        if o["market"] is None and f["model_market_gap_home"] is not None:
            problems.append("%s.model_market_gap_home: must be null without a market" % fw)

    # injury reports (D18)
    _check_injuries(o["injuries"], ow + ".injuries", problems, params_injury)

    # rationale
    if not isinstance(o["rationale"], list) or not all(k in RATIONALE_KEYS for k in o["rationale"]):
        problems.append("%s.rationale: list of keys from %s" % (ow, RATIONALE_KEYS))
    if not isinstance(o["rationale_sentences"], list) or not all(isinstance(s, str) and s for s in o["rationale_sentences"]):
        problems.append("%s.rationale_sentences: list of non-empty strings" % ow)
    elif isinstance(o["rationale"], list) and len(o["rationale"]) != len(o["rationale_sentences"]):
        problems.append("%s: rationale and rationale_sentences must have the same length" % ow)
    if isinstance(o["rationale"], list) and o["rationale"]:
        if o["rationale"][0] != "rating" or o["rationale"][-1] != "verdict":
            problems.append("%s.rationale: must start with 'rating' and end with 'verdict'" % ow)
        if all(k in RATIONALE_KEYS for k in o["rationale"]):
            _check_rationale_keys(g, o, ow, problems)

    # edge chip
    if o["edge"] is not None and _check_keys(o["edge"], EDGE_KEYS, ow + ".edge", problems):
        if o["edge"]["flag"] is not True:
            problems.append("%s.edge.flag: must be true when edge is present (else edge is null)" % ow)
        if o["edge"]["reason_key"] not in EDGE_REASON_KEYS:
            problems.append("%s.edge.reason_key: %r not in %s" % (ow, o["edge"]["reason_key"], EDGE_REASON_KEYS))
        if not isinstance(o["edge"]["reason_text"], str) or not o["edge"]["reason_text"]:
            problems.append("%s.edge.reason_text: non-empty string" % ow)
        if o["market"] is None:
            problems.append("%s.edge: must be null without a market" % ow)

    # result
    r = g["result"]
    if g["status"] == "final":
        if r is None:
            problems.append("%s.result: required when status is final" % where)
        elif _check_keys(r, RESULT_KEYS, where + ".result", problems):
            rw = where + ".result"
            for k in ("home_score", "away_score", "margin"):
                if not _is_int(r[k]):
                    problems.append("%s.%s: int" % (rw, k))
            if r["winner"] not in (g["home"], g["away"], "tie"):
                problems.append("%s.winner: home, away or 'tie'" % rw)
            if r["hit"] is not None and not isinstance(r["hit"], bool):
                problems.append("%s.hit: bool or null" % rw)
            if _is_int(r["home_score"]) and _is_int(r["away_score"]):
                margin = r["home_score"] - r["away_score"]
                if r["margin"] != margin:
                    problems.append("%s.margin must equal home_score - away_score" % rw)
                expect_w = g["home"] if margin > 0 else g["away"] if margin < 0 else "tie"
                if r["winner"] != expect_w:
                    problems.append("%s.winner does not match the scores" % rw)
                if expect_w == "tie" and r["hit"] is not None:
                    problems.append("%s.hit must be null for a tie" % rw)
                if expect_w != "tie" and r["hit"] != (o["pick"] == expect_w):
                    problems.append("%s.hit does not match pick vs winner" % rw)
            for k in ("pred_margin_error", "pred_total_error"):
                if not _is_num(r[k]):
                    problems.append("%s.%s: number" % (rw, k))
    elif r is not None:
        problems.append("%s.result: must be null when status is scheduled" % where)


def _check_team(t, i: int, problems: list[str]) -> None:
    where = "teams[%d]" % i
    if not _check_keys(t, TEAM_KEYS, where, problems):
        return
    where = "teams[%d](%s)" % (i, t["team"])
    if t["team"] not in TEAM_SET:
        problems.append("%s.team: not a canonical team" % where)
        return
    meta = next(m for m in TEAM_META if m["code"] == t["team"])
    for k, mk in (("name", "name"), ("city", "city"), ("conference", "conference"), ("division", "division"), ("full_name", "full_name")):
        if t[k] != meta[mk]:
            problems.append("%s.%s: %r does not match teams.json %r" % (where, k, t[k], meta[mk]))
    if not isinstance(t["aliases"], list) or not all(isinstance(a, str) for a in t["aliases"]):
        problems.append("%s.aliases: list of strings" % where)
    if not _is_num(t["elo"]):
        problems.append("%s.elo: number" % where)
    if not _is_int(t["rank"]) or not 1 <= t["rank"] <= 32:
        problems.append("%s.rank: int 1..32" % where)
    rec = t["record"]
    if not isinstance(rec, dict) or set(rec) != {"w", "l", "t"} or not all(_is_int(rec[k]) and rec[k] >= 0 for k in rec):
        problems.append("%s.record: {w,l,t} non-negative ints" % where)
    if not isinstance(t["trend"], list) or not all(
        isinstance(p, list) and len(p) == 2 and _is_int(p[0]) and _is_num(p[1]) for p in t["trend"]
    ):
        problems.append("%s.trend: list of [week, elo] pairs" % where)
    if t["qb"] is not None:
        q = t["qb"]
        if not isinstance(q, dict) or set(q) != {"id", "name", "q"}:
            problems.append("%s.qb: {id, name, q} or null" % where)
        elif not isinstance(q["id"], str) or not isinstance(q["name"], str) or not _is_num(q["q"]):
            problems.append("%s.qb: id/name strings, q number" % where)
    sim = t["simulation"]
    if _check_keys(sim, SIM_KEYS, where + ".simulation", problems):
        if not _is_int(sim["runs"]) or sim["runs"] < 1:
            problems.append("%s.simulation.runs: positive int" % where)
        if not _is_num(sim["projected_wins"]) or sim["projected_wins"] < 0:
            problems.append("%s.simulation.projected_wins: number >= 0" % where)
        for k in ("playoffs", "division", "seed1", "super_bowl"):
            if not _is_prob(sim[k]):
                problems.append("%s.simulation.%s: probability" % (where, k))


def _check_agg(obj, where: str, problems: list[str], required: tuple = ("n",)) -> bool:
    if not isinstance(obj, dict):
        problems.append("%s: expected object" % where)
        return False
    for k in required:
        if k not in obj:
            problems.append("%s: missing %r" % (where, k))
    if "n" in obj and (not _is_int(obj["n"]) or obj["n"] < 0):
        problems.append("%s.n: non-negative int" % where)
    return True


def _check_track_record(tr, problems: list[str]) -> None:
    where = "track_record"
    if not _check_keys(tr, TRACK_RECORD_KEYS, where, problems):
        return
    ov = tr["overall"]
    if _check_agg(ov, where + ".overall", problems, ("n", "hits", "losses", "pushes", "accuracy", "market_n", "market_hits", "market_accuracy")):
        if all(_is_int(ov.get(k)) for k in ("n", "hits", "losses", "pushes")) and ov["hits"] + ov["losses"] + ov["pushes"] != ov["n"]:
            problems.append("%s.overall: hits + losses + pushes must equal n" % where)
        if ov.get("accuracy") is not None and not _is_prob(ov["accuracy"]):
            problems.append("%s.overall.accuracy: probability or null" % where)
        if ov.get("n") == 0 and ov.get("accuracy") is not None:
            problems.append("%s.overall.accuracy: must be null when n is 0" % where)
    bt = tr["by_tier"]
    if not isinstance(bt, list) or [x.get("tier") for x in bt if isinstance(x, dict)] != list(TIERS):
        problems.append("%s.by_tier: list of 4 entries in tier order %s" % (where, TIERS))
    else:
        for x in bt:
            _check_agg(x, "%s.by_tier[%s]" % (where, x["tier"]), problems, ("tier", "n", "hits", "losses", "pushes", "accuracy", "market_n", "market_hits", "market_accuracy"))
    bl = tr["by_injury_load"]
    if not isinstance(bl, list) or [x.get("load") for x in bl if isinstance(x, dict)] != list(INJURY_LOADS):
        problems.append("%s.by_injury_load: list of 3 entries in load order %s" % (where, INJURY_LOADS))
    else:
        for x in bl:
            if _check_agg(x, "%s.by_injury_load[%s]" % (where, x["load"]), problems, ("load", "n", "hits", "losses", "pushes", "accuracy", "market_n", "market_hits", "market_accuracy")):
                if all(_is_int(x.get(k)) for k in ("n", "hits", "losses", "pushes")) and x["hits"] + x["losses"] + x["pushes"] != x["n"]:
                    problems.append("%s.by_injury_load[%s]: hits + losses + pushes must equal n" % (where, x["load"]))
        if isinstance(ov, dict) and _is_int(ov.get("n")) and all(_is_int(x.get("n")) for x in bl if isinstance(x, dict)):
            if sum(x["n"] for x in bl) != ov["n"]:
                problems.append("%s.by_injury_load: bucket counts must sum to overall.n" % where)
    bw = tr["by_week"]
    if not isinstance(bw, list):
        problems.append("%s.by_week: list" % where)
    else:
        for i, x in enumerate(bw):
            _check_agg(x, "%s.by_week[%d]" % (where, i), problems, ("season", "week", "n", "hits", "cum_n", "cum_hits", "cum_accuracy", "market_n", "market_hits", "market_cum_n", "market_cum_hits", "market_cum_accuracy"))
    cal = tr["calibration"]
    if _check_agg(cal, where + ".calibration", problems, ("n", "bins", "error", "error_n")):
        bins = cal.get("bins")
        if not isinstance(bins, list) or len(bins) != 10:
            problems.append("%s.calibration.bins: exactly 10 bins" % where)
        else:
            for i, b in enumerate(bins):
                bwh = "%s.calibration.bins[%d]" % (where, i)
                if _check_agg(b, bwh, problems, ("lo", "hi", "n", "mean_p", "hit_rate", "low_sample")):
                    if abs(b["lo"] - i / 10) > 1e-9 or abs(b["hi"] - (i + 1) / 10) > 1e-9:
                        problems.append("%s: edges must be [i/10, (i+1)/10]" % bwh)
                    if b["low_sample"] != (b["n"] < 10):
                        problems.append("%s.low_sample must equal (n < 10)" % bwh)
                    if b["n"] == 0 and (b["mean_p"] is not None or b["hit_rate"] is not None):
                        problems.append("%s: mean_p/hit_rate must be null when n is 0" % bwh)
            if _is_int(cal.get("n")) and all(_is_int(b.get("n")) for b in bins if isinstance(b, dict)):
                if sum(b["n"] for b in bins) != cal["n"]:
                    problems.append("%s.calibration: bin counts must sum to n" % where)
    _check_agg(tr["score_error"], where + ".score_error", problems, ("n", "margin_mae", "total_mae", "market_n", "market_margin_mae", "market_total_mae"))
    _check_agg(tr["clv"], where + ".clv", problems, ("n", "mean_pp", "share_toward_pick"))
    un = tr["units"]
    if isinstance(un, dict):
        for k in ("all", "ev_positive"):
            _check_agg(un.get(k), "%s.units.%s" % (where, k), problems, ("n", "hits", "losses", "pushes", "units", "roi", "mean_break_even"))
        if not _is_num(un.get("break_even_minus_110")) or abs(un["break_even_minus_110"] - 110 / 210) > 1e-9:
            problems.append("%s.units.break_even_minus_110: must be 110/210" % where)
    else:
        problems.append("%s.units: object" % where)


def injury_rationale_triggers(injuries) -> bool:
    """CONTRACT 2.6b trigger: any starter Out or Doubtful on either side (QBs never count)."""
    if not isinstance(injuries, dict):
        return False
    n = 0
    for side in ("home", "away"):
        rep = injuries.get(side)
        if isinstance(rep, dict) and rep.get("available"):
            n += int(rep.get("starters_out") or 0) + int(rep.get("starters_doubtful") or 0)
    return n >= 1


def expected_rationale_keys(game: dict) -> list[str]:
    """CONTRACT 2.6: the rationale keys a game's factors require, in RATIONALE_KEYS order.

    rating and verdict always; home_field iff not neutral; quarterback iff both starters are
    listed and one differs from the usual starter, or is a first start, or |qb_elo| >= 30;
    rest iff |rest_diff| >= 3; injuries iff a starter is Out or Doubtful on either side
    (CONTRACT 2.6b counts, which exclude quarterbacks); form iff either |form_*_3| >= 40;
    market iff a market is present and |model_market_gap_pick| >= 0.05. Pure; used by
    validate_model and tests."""
    o = game["official"]
    f = o["factors"]
    keys = ["rating"]
    if not game.get("neutral"):
        keys.append("home_field")
    qh, qa = f.get("qb_home"), f.get("qb_away")
    if qh is not None and qa is not None:
        change = any(bool(q.get("differs_from_usual")) or bool(q.get("first_start")) for q in (qh, qa))
        if change or abs(f.get("qb_elo") or 0.0) >= RATIONALE_QB_GAP:
            keys.append("quarterback")
    if f.get("rest_diff") is not None and abs(f["rest_diff"]) >= RATIONALE_REST_GAP:
        keys.append("rest")
    if injury_rationale_triggers(o.get("injuries")):
        keys.append("injuries")
    if any(f.get(k) is not None and abs(f[k]) >= RATIONALE_FORM_MOVE for k in ("form_home_3", "form_away_3")):
        keys.append("form")
    gap = f.get("model_market_gap_pick")
    if o.get("market") is not None and gap is not None and abs(gap) >= RATIONALE_MARKET_GAP:
        keys.append("market")
    keys.append("verdict")
    return keys


def _check_rationale_keys(game: dict, o: dict, ow: str, problems: list) -> None:
    """Order and trigger rules of CONTRACT 2.6 (keys already known to be valid names)."""
    keys = o["rationale"]
    if len(set(keys)) != len(keys):
        problems.append("%s.rationale: duplicate keys" % ow)
        return
    order = [RATIONALE_KEYS.index(k) for k in keys]
    if order != sorted(order):
        problems.append("%s.rationale: keys must follow the order %s" % (ow, RATIONALE_KEYS))
    f = o.get("factors")
    if not isinstance(f, dict) or set(f) != FACTOR_KEYS:
        return  # factor problems are reported separately
    try:
        expected = expected_rationale_keys(game)
    except (TypeError, AttributeError):
        return
    if sorted(keys) != sorted(expected):
        problems.append("%s.rationale: %s does not match the factors' triggers %s" % (ow, keys, expected))


def validate_model(model: dict) -> list[str]:
    """Validate a model.json object against docs/CONTRACT.md. Returns problems (empty = valid)."""
    problems: list[str] = []
    if not isinstance(model, dict):
        return ["model: expected an object"]
    if not _check_keys(model, TOP_KEYS, "model", problems):
        return problems
    sv = model["schema_version"]
    if not isinstance(sv, str) or sv.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        problems.append("schema_version: %r is not major version %s" % (sv, SCHEMA_VERSION.split(".")[0]))

    b = model["build"]
    if _check_keys(b, BUILD_KEYS, "build", problems):
        if not isinstance(b["id"], str) or not b["id"]:
            problems.append("build.id: non-empty string")
        if not isinstance(b["built_at"], str) or not _ISO_UTC.match(b["built_at"]):
            problems.append("build.built_at: ISO-8601 UTC")
        if b["official_odds_at"] is not None and (not isinstance(b["official_odds_at"], str) or not _ISO_UTC.match(b["official_odds_at"])):
            problems.append("build.official_odds_at: ISO-8601 UTC or null")
        if not isinstance(b["league_date"], str) or not _ISO_DATE.match(b["league_date"]):
            problems.append("build.league_date: 'YYYY-MM-DD'")
        if b["next_league_date"] is not None and (not isinstance(b["next_league_date"], str) or not _ISO_DATE.match(b["next_league_date"])):
            problems.append("build.next_league_date: 'YYYY-MM-DD' or null")
        if b["odds_source"] not in MARKET_SOURCES:
            problems.append("build.odds_source: %r not in %s" % (b["odds_source"], MARKET_SOURCES))
        d = b["data"]
        if _check_keys(d, frozenset(["source_url", "nflverse_sha256", "rows", "max_result_date", "fetched"]), "build.data", problems):
            if not isinstance(d["nflverse_sha256"], str) or not re.match(r"^[0-9a-f]{64}$", d["nflverse_sha256"]):
                problems.append("build.data.nflverse_sha256: 64 hex chars")
            if not _is_int(d["rows"]) or d["rows"] < 0:
                problems.append("build.data.rows: non-negative int")
            if d["max_result_date"] is not None and not _ISO_DATE.match(str(d["max_result_date"])):
                problems.append("build.data.max_result_date: 'YYYY-MM-DD' or null")
            if not isinstance(d["fetched"], bool):
                problems.append("build.data.fetched: bool")
        bi = b["injuries"]
        if _check_keys(bi, BUILD_INJURIES_KEYS, "build.injuries", problems):
            if not isinstance(bi["available"], bool):
                problems.append("build.injuries.available: bool")
            if not _is_int(bi["injuries_rows"]) or bi["injuries_rows"] < 0:
                problems.append("build.injuries.injuries_rows: non-negative int")
            if bi["depth_chart_as_of"] is not None and (not isinstance(bi["depth_chart_as_of"], str) or not _ISO_UTC.match(bi["depth_chart_as_of"])):
                problems.append("build.injuries.depth_chart_as_of: ISO-8601 UTC or null")
            if not _is_int(bi["teams_with_report_for_next_slate"]) or not 0 <= bi["teams_with_report_for_next_slate"] <= 32:
                problems.append("build.injuries.teams_with_report_for_next_slate: int in 0..32")
            if bi["available"] is False and (bi["depth_chart_as_of"] is not None or bi["teams_with_report_for_next_slate"] != 0):
                problems.append("build.injuries: depth_chart_as_of must be null and teams_with_report_for_next_slate 0 when unavailable")

    p = model["params"]
    tier_bounds = TIER_BOUNDS
    params_injury = None
    if _check_keys(p, frozenset(["elo", "qb", "blend", "tier_bounds", "score", "sim", "books", "injury"]), "params", problems):
        params_injury = p["injury"]
        _check_params_injury(params_injury, problems)
        _check_keys(p["elo"], frozenset(["k", "hfa", "rest_per_day", "reversion", "tie_multiplier"]), "params.elo", problems)
        _check_keys(p["qb"], frozenset(["enabled", "k", "new_qb", "annual_reversion", "inactivity_penalty", "usual_starter_window"]), "params.qb", problems)
        _check_keys(p["blend"], frozenset(["model_weight", "fit_seasons", "fit_metric", "fitted_model_weight", "pinned"]), "params.blend", problems)
        if isinstance(p["blend"], dict) and not _is_prob(p["blend"].get("model_weight")):
            problems.append("params.blend.model_weight: in [0,1]")
        if _check_keys(p["tier_bounds"], frozenset(["lean", "strong", "lockish"]), "params.tier_bounds", problems):
            tb = p["tier_bounds"]
            if all(_is_prob(tb[k]) for k in tb) and tb["lean"] < tb["strong"] < tb["lockish"]:
                tier_bounds = tb
            else:
                problems.append("params.tier_bounds: need 0 <= lean < strong < lockish <= 1")
        _check_keys(p["score"], frozenset(["points_per_elo", "league_average_total", "league_average_season"]), "params.score", problems)
        _check_keys(p["sim"], frozenset(["runs", "seed"]), "params.sim", problems)
        if not isinstance(p["books"], list) or not all(isinstance(x, str) for x in p["books"]):
            problems.append("params.books: list of book keys")
    default_books = tuple(p["books"]) if isinstance(p, dict) and isinstance(p.get("books"), list) else DEFAULT_BOOKS

    ev = model["evaluation"]
    if _check_keys(ev, frozenset(["season", "samples", "model", "model_no_qb", "market", "blend"]), "evaluation", problems):
        if not _is_int(ev["season"]):
            problems.append("evaluation.season: int")
        _check_keys(ev["samples"], frozenset(["model", "market_comparable"]), "evaluation.samples", problems)
        for k in ("model", "model_no_qb", "market", "blend"):
            if _check_keys(ev[k], frozenset(["brier", "logloss", "accuracy"]), "evaluation." + k, problems):
                for m in ("brier", "logloss", "accuracy"):
                    if ev[k][m] is not None and (not _is_num(ev[k][m]) or ev[k][m] < 0):
                        problems.append("evaluation.%s.%s: non-negative number or null" % (k, m))

    games = model["games"]
    if not isinstance(games, list):
        problems.append("games: list")
    else:
        ids = [g.get("game_id") for g in games if isinstance(g, dict)]
        if len(ids) != len(set(ids)):
            problems.append("games: duplicate game_id")
        for i, g in enumerate(games):
            _check_game(g, i, problems, tier_bounds, default_books, params_injury)

    teams = model["teams"]
    if not isinstance(teams, list) or len(teams) != 32:
        problems.append("teams: must be a list of exactly 32 entries")
    else:
        codes = [t.get("team") for t in teams if isinstance(t, dict)]
        if sorted(codes) != list(TEAMS):
            problems.append("teams: must contain each canonical team exactly once")
        ranks = sorted(t.get("rank") for t in teams if isinstance(t, dict))
        if ranks != list(range(1, 33)):
            problems.append("teams: ranks must be a permutation of 1..32")
        for i, t in enumerate(teams):
            _check_team(t, i, problems)
        try:
            for conf in CONFERENCES:
                tot = sum(t["simulation"]["playoffs"] for t in teams if t["conference"] == conf)
                if abs(tot - 7.0) > 0.05:
                    problems.append("teams: %s playoff probabilities sum to %.4f, expected ~7" % (conf, tot))
            sb = sum(t["simulation"]["super_bowl"] for t in teams)
            if abs(sb - 1.0) > 1e-6:
                problems.append("teams: super_bowl probabilities sum to %.6f, expected 1" % sb)
        except (KeyError, TypeError):
            pass  # already reported by _check_team

    sc = model["scorecard"]
    if _check_keys(sc, frozenset(["league_date", "games_on_slate", "season", "holdout"]), "scorecard", problems):
        if not _is_int(sc["games_on_slate"]) or sc["games_on_slate"] < 0:
            problems.append("scorecard.games_on_slate: non-negative int")
        _check_agg(sc["season"], "scorecard.season", problems, ("season", "n", "hits", "losses", "pushes", "accuracy", "market_n", "market_accuracy"))
        _check_keys(sc["holdout"], frozenset(["season", "n", "model_accuracy", "market_accuracy", "model_brier", "market_brier"]), "scorecard.holdout", problems)

    _check_track_record(model["track_record"], problems)
    return problems


def validate_overlay(overlay: dict) -> list[str]:
    """Validate a Worker/normalized overlay object (D6). Returns problems (empty = valid)."""
    problems: list[str] = []
    if not _check_keys(overlay, frozenset(["schema_version", "fetched_at", "degraded", "canonical", "source", "games"]), "overlay", problems):
        return problems
    sv = overlay["schema_version"]
    if not isinstance(sv, str) or sv.split(".")[0] != OVERLAY_SCHEMA_VERSION.split(".")[0]:
        problems.append("overlay.schema_version: major version mismatch")
    if overlay["fetched_at"] is not None and (not isinstance(overlay["fetched_at"], str) or not _ISO_UTC.match(overlay["fetched_at"])):
        problems.append("overlay.fetched_at: ISO-8601 UTC or null")
    for k in ("degraded", "canonical"):
        if not isinstance(overlay[k], bool):
            problems.append("overlay.%s: bool" % k)
    if overlay["source"] not in ("worker", "build"):
        problems.append("overlay.source: 'worker' or 'build'")
    if not isinstance(overlay["games"], list):
        problems.append("overlay.games: list")
        return problems
    for i, g in enumerate(overlay["games"]):
        w = "overlay.games[%d]" % i
        if not _check_keys(g, frozenset(["home", "away", "commence_time", "books"]), w, problems):
            continue
        for k in ("home", "away"):
            if not isinstance(g[k], str) or not g[k]:
                problems.append("%s.%s: non-empty string" % (w, k))
            elif overlay["canonical"] is True and g[k] not in TEAM_SET:
                problems.append("%s.%s: %r not canonical although overlay.canonical is true" % (w, k, g[k]))
        if not isinstance(g["commence_time"], str) or not _ISO_UTC.match(g["commence_time"]):
            problems.append("%s.commence_time: ISO-8601 UTC" % w)
        if not isinstance(g["books"], list):
            problems.append("%s.books: list" % w)
            continue
        for j, b in enumerate(g["books"]):
            bw = "%s.books[%d]" % (w, j)
            if not _check_keys(b, BOOK_KEYS, bw, problems):
                continue
            for k in ("home_ml", "away_ml"):
                if b[k] is not None and (not _is_int(b[k]) or abs(b[k]) < 100):
                    problems.append("%s.%s: int with |x| >= 100, or null" % (bw, k))
            for k in ("home_spread", "total"):
                if b[k] is not None and not _is_num(b[k]):
                    problems.append("%s.%s: number or null" % (bw, k))
    return problems
