"""The daily build (D15, CONTRACT 6.7): orchestration only, no model math.

    python3 -m src.build            # == make build

Pipeline: fetch games.csv (or use the cache under GRIDIRON_OFFLINE=1) -> load -> Elo replay ->
one official odds call (ODDS_API_KEY) or CSV closing lines -> predict every game of the current
season -> season simulation -> ledger snapshots + grades + reducer -> assemble model.json ->
schema.validate_model (any problem aborts, nothing is written) -> append picks.jsonl ->
write model.json and index.html atomically.

Environment: GRIDIRON_OFFLINE (skip the download), ODDS_API_KEY (the single Odds API call),
GRIDIRON_ODDS_URL (the optional live-overlay Worker URL, embedded by src/render.py). Nothing else.

Every helper below is pure except ``run``/``main`` (I/O) and ``official_market_inputs`` (one
network call through ``market.fetch_official_odds``). Numbers are never rounded here.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import data, elo, evaluate, ledger, market, predict, qb, render, schema, simulate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES_PATH = os.path.join(ROOT, "data", "games.csv")
PARAMS_PATH = os.path.join(ROOT, "params.json")
PICKS_PATH = os.path.join(ROOT, "picks.jsonl")
MODEL_PATH = os.path.join(ROOT, "model.json")
INDEX_PATH = os.path.join(ROOT, "index.html")

PARAM_KEYS = ("elo", "qb", "blend", "tier_bounds", "score", "sim", "books")
CSV_CLOSE_KEYS = ("home_moneyline", "away_moneyline", "spread_line", "total_line")


class BuildError(Exception):
    """Raised for any condition that must abort the build (loud failure, nothing written)."""


# ----------------------------------------------------------------------------
# Small pure helpers
# ----------------------------------------------------------------------------


def iso_utc(dt: datetime) -> str:
    """ISO-8601 UTC with a trailing Z at second precision (CONTRACT 0)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def league_date_at(iso: str) -> str:
    """The America/New_York calendar date of a UTC instant (D7)."""
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(schema.LEAGUE_TZ)).strftime("%Y-%m-%d")


def build_id(built_at: str, sha256: str) -> str:
    """CONTRACT 2.2: built_at compact + '-' + first 7 hex of the data hash."""
    return built_at.replace("-", "").replace(":", "") + "-" + sha256[:7]


def is_offline(env: dict | None = None) -> bool:
    """GRIDIRON_OFFLINE set to anything but '', '0', 'false', 'no' means use the cache."""
    env = os.environ if env is None else env
    return (env.get("GRIDIRON_OFFLINE") or "").strip().lower() not in ("", "0", "false", "no")


def current_season(games: list[dict]) -> int:
    return max(g["season"] for g in games)


def season_games(games: list[dict], season: int) -> list[dict]:
    """Every row of ``season`` in sort_key order (REG now; playoff rows join when published)."""
    return [g for g in games if g["season"] == season]


def current_ratings(replay_result: dict, games: list[dict], season: int, params: dict) -> dict:
    """Team Elo 'now': the replay's final ratings, rolled over once if ``season`` has not started."""
    ratings = dict(replay_result["ratings"])
    played_seasons = [g["season"] for g in games if g["played"]]
    if played_seasons and max(played_seasons) < season:
        rev = float(params["elo"]["reversion"])
        ratings = {code: elo.rollover(r, rev) for code, r in ratings.items()}
    return ratings


def next_league_date(entries: list[dict], league_date: str) -> str | None:
    """First league_date >= ``league_date`` with a scheduled game, else None (CONTRACT 2.2)."""
    dates = [g["league_date"] for g in entries if g["status"] == "scheduled" and g["league_date"] >= league_date]
    return min(dates) if dates else None


def params_block(params_file: dict) -> dict:
    """The section-2.3 params object (exactly the seven keys) from params.json."""
    missing = [k for k in PARAM_KEYS if k not in params_file]
    if missing:
        raise BuildError("params.json is missing %s (run make tune)" % missing)
    return {k: params_file[k] for k in PARAM_KEYS}


def game_market(game: dict, overlay_game: dict | None, odds_at: str | None, built_at: str) -> dict | None:
    """CONTRACT 2.8 market for one game: the feed's books when the game matched the official
    odds snapshot and at least one book priced both sides, else the CSV closing lines, else None."""
    if overlay_game is not None and odds_at is not None:
        books = overlay_game.get("books") or []
        if any(b.get("home_ml") is not None and b.get("away_ml") is not None for b in books):
            return market.market_from_books(books, "odds_api", odds_at)
    return market.market_from_csv(game, built_at)


def result_block(game: dict, official: dict) -> dict | None:
    """CONTRACT 2.7 result for a played game, None otherwise."""
    if not game["played"]:
        return None
    hs, as_ = game["home_score"], game["away_score"]
    margin = hs - as_
    winner = game["home"] if margin > 0 else game["away"] if margin < 0 else "tie"
    return {
        "home_score": hs,
        "away_score": as_,
        "winner": winner,
        "margin": margin,
        "hit": None if winner == "tie" else official["pick"] == winner,
        "pred_margin_error": official["blend_spread"] - margin,
        "pred_total_error": official["total_used"] - (hs + as_),
    }


def game_entry(game: dict, prediction: dict, frozen_snapshot: str | None = None) -> dict:
    """CONTRACT 2.5 games[] entry from a normalized game and predict_game's output."""
    official = prediction["official"]
    return {
        "frozen_snapshot": frozen_snapshot,
        "game_id": game["game_id"],
        "season": game["season"],
        "game_type": game["game_type"],
        "week": game["week"],
        "league_date": game["league_date"],
        "kickoff_utc": game["kickoff_utc"],
        "time_known": game["time_known"],
        "away": game["away"],
        "home": game["home"],
        "neutral": game["neutral"],
        "status": "final" if game["played"] else "scheduled",
        "availability": prediction["availability"],
        "official": official,
        "result": result_block(game, official),
    }


def predict_context(replay_result: dict, ratings_now: dict, params: dict) -> dict:
    """CONTRACT 6.3 context shared by every predict_game call."""
    return {
        "ranks": elo.ranks(ratings_now),
        "names": {t["code"]: t for t in schema.TEAM_META},
        "form": {code: elo.form_delta(replay_result["history"], replay_result["pregame"], code) for code in schema.TEAMS},
        "usual_starter": replay_result["usual_starter"],
        "qb": replay_result["qb"],
        "books": list(params["books"]),
    }


def team_entries(replay_result: dict, ratings_now: dict, season: int, sim: dict,
                 params: dict | None = None, today: str | None = None) -> list[dict]:
    """CONTRACT 2.10 teams[] (32, sorted by code) with ranks, trend, usual starter and simulation.

    ``teams[].qb.q`` is the usual starter's Q as of ``today`` (lazy inactivity decay, the same
    value predict.py compares a listed starter against); the stored Q when ``params``/``today``
    are not given."""
    ranks = elo.ranks(ratings_now)
    out = []
    for meta in schema.TEAM_META:
        code = meta["code"]
        usual = replay_result["usual_starter"].get(code)
        qb_entry = None
        if usual is not None:
            state = replay_result["qb"].get(usual["id"])
            if state is not None and params is not None and today is not None:
                q = qb.effective_q(state, today, params)
            else:
                q = float((state or {}).get("q", 0.0))
            qb_entry = {"id": usual["id"], "name": usual["name"], "q": float(q)}
        trend = [[h[1], h[3]] for h in replay_result["history"].get(code, []) if h[0] == season]
        out.append({
            "team": code,
            "name": meta["name"],
            "city": meta["city"],
            "conference": meta["conference"],
            "division": meta["division"],
            "full_name": meta["full_name"],
            "aliases": list(meta.get("aliases") or []),
            "elo": ratings_now[code],
            "rank": ranks[code],
            "record": dict(replay_result["records"][code]),
            "trend": trend,
            "qb": qb_entry,
            "simulation": None,
        })
    return simulate.attach(out, sim)


def ledger_games(entries: list[dict], by_id: dict) -> list[dict]:
    """games[] entries extended with the CSV close lines the ledger needs (CONTRACT 6.6 note)."""
    out = []
    for e in entries:
        g = by_id[e["game_id"]]
        out.append({**{k: g[k] for k in CSV_CLOSE_KEYS}, **e})
    return out


def scorecard_block(build: dict, entries: list[dict], season_agg: dict, evaluation: dict) -> dict:
    """CONTRACT 2.11."""
    slate = build["league_date"] if any(g["league_date"] == build["league_date"] for g in entries) else build["next_league_date"]
    if slate is None:
        slate = build["league_date"]
    return {
        "league_date": slate,
        "games_on_slate": sum(1 for g in entries if g["league_date"] == slate),
        "season": season_agg,
        "holdout": {
            "season": evaluation["season"],
            "n": evaluation["samples"]["model"],
            "model_accuracy": evaluation["model"]["accuracy"],
            "market_accuracy": evaluation["market"]["accuracy"],
            "model_brier": evaluation["model"]["brier"],
            "market_brier": evaluation["market"]["brier"],
        },
    }


# ----------------------------------------------------------------------------
# Odds (the one network call) and assembly
# ----------------------------------------------------------------------------


def official_market_inputs(api_key: str | None, games: list[dict], fetch=None) -> dict:
    """Make the single official Odds API call when a key is set (D6).

    Returns {"odds_source", "official_odds_at", "matched": {game_id: overlay_game}}. Any failure
    (no key, HTTP error, bad payload) falls back to CSV lines; nothing is retried.
    """
    if not api_key:
        return {"odds_source": "csv", "official_odds_at": None, "matched": {}}
    r = (fetch or market.fetch_official_odds)(api_key)
    if r is None:
        return {"odds_source": "csv", "official_odds_at": None, "matched": {}}
    overlay = market.normalize_odds_api(r["payload"], schema.TEAM_META, fetched_at=r["fetched_at"])
    problems = schema.validate_overlay(overlay)
    if problems:
        print("odds api: normalized overlay failed validation (%s); using CSV lines" % problems[:3], file=sys.stderr)
        return {"odds_source": "csv", "official_odds_at": None, "matched": {}}
    return {"odds_source": "odds_api", "official_odds_at": r["fetched_at"], "matched": market.match_overlay(games, overlay)}


def assemble(games: list[dict], params_file: dict, fetch_report: dict, built_at: str, odds: dict, events: list[dict]) -> dict:
    """Everything from normalized games to a validated-shape model plus the new ledger events.

    Pure given its inputs. Returns {"model", "new_snapshots", "new_grades"}; the caller validates
    the model and does the writing.
    """
    params = params_block(params_file)
    season = current_season(games)
    rows = season_games(games, season)
    by_id = {g["game_id"]: g for g in games}

    rep = elo.replay(games, params)
    ratings_now = current_ratings(rep, games, season, params)
    context = predict_context(rep, ratings_now, params)

    entries = []
    for g in rows:
        mkt = game_market(g, odds["matched"].get(g["game_id"]), odds["official_odds_at"], built_at)
        pred = predict.predict_game(g, rep["pregame"][g["game_id"]], mkt, params, context)
        frozen = None
        if not ledger._before(built_at, g["kickoff_utc"]):
            # Kicked off: the card shows exactly what the ledger graded (CONTRACT 2.6a), never a
            # recomputation from the closing line.
            snap = ledger.latest_prekick_snapshot(events, g["game_id"], g["kickoff_utc"])
            if snap is not None:
                pred = dict(pred, official=predict.freeze_official(pred["official"], snap, g, rep["pregame"][g["game_id"]], params, context))
                pred["availability"] = dict(pred["availability"], market=pred["official"]["market"] is not None)
                frozen = snap["id"]
        entries.append(game_entry(g, pred, frozen))

    sim = simulate.simulate(schema.TEAM_META, [g for g in rows if not g["played"]], ratings_now, params,
                            runs=int(params["sim"]["runs"]), seed=int(params["sim"]["seed"]), records=rep["records"])
    league_date = league_date_at(built_at)
    teams = team_entries(rep, ratings_now, season, sim, params, league_date)
    build = {
        "id": build_id(built_at, fetch_report["sha256"]),
        "built_at": built_at,
        "official_odds_at": odds["official_odds_at"],
        "league_date": league_date,
        "next_league_date": next_league_date(entries, league_date),
        "odds_source": odds["odds_source"],
        "data": {
            "source_url": fetch_report["url"],
            "nflverse_sha256": fetch_report["sha256"],
            "rows": fetch_report["rows"],
            "max_result_date": data.max_result_date(games),
            "fetched": bool(fetch_report["fetched"]),
        },
    }

    lgames = ledger_games(entries, by_id)
    new_snapshots = ledger.append_snapshots(events, lgames, build)
    new_grades = ledger.append_grades(events + new_snapshots, lgames, built_at)
    index = {g["game_id"]: g for g in games}  # earlier seasons: season/week/home/close lines
    index.update({g["game_id"]: g for g in lgames})
    reduced = ledger.reduce(events + new_snapshots + new_grades, index)

    # SPEC: the holdout report runs with every build (one extra no-QB replay, ~0.05 s), so a
    # revised nflverse history never leaves params.json's tune-time copy stale on the page.
    evaluation = evaluate.holdout(games, params, replays={"model": rep})

    model = {
        "schema_version": schema.SCHEMA_VERSION,
        "build": build,
        "params": params,
        "evaluation": evaluation,
        "games": entries,
        "teams": teams,
        "scorecard": scorecard_block(build, entries, reduced["scorecard_season"], evaluation),
        "track_record": reduced["track_record"],
    }
    return {"model": model, "new_snapshots": new_snapshots, "new_grades": new_grades}


# ----------------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------------


def write_json_atomic(obj: dict, path: str) -> None:
    """Temp file + os.replace; full precision, NaN rejected."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, allow_nan=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def run(games_path: str = GAMES_PATH, params_path: str = PARAMS_PATH, picks_path: str = PICKS_PATH,
        model_path: str = MODEL_PATH, index_path: str = INDEX_PATH, offline: bool | None = None,
        api_key: str | None = None, odds_url: str | None = None, now: datetime | None = None,
        fetch_odds=None, log=print) -> dict:
    """The whole build. Returns a summary dict; raises BuildError / DataError on failure.

    Nothing is written unless ``schema.validate_model`` returns no problems. Write order:
    picks.jsonl (append-only), model.json (atomic), index.html (atomic).
    """
    t0 = time.perf_counter()
    offline = is_offline() if offline is None else offline
    api_key = os.environ.get("ODDS_API_KEY") if api_key is None else api_key
    built_at = iso_utc(now or datetime.now(timezone.utc))

    report = data.fetch_games(schema.NFLVERSE_URL, games_path, offline=offline)
    games = data.load_games(report["path"])
    with open(params_path, encoding="utf-8") as fh:
        params_file = json.load(fh)
    events = ledger.read_events(picks_path)
    odds = official_market_inputs(api_key, games, fetch=fetch_odds)

    out = assemble(games, params_file, report, built_at, odds, events)
    model = out["model"]
    problems = schema.validate_model(model)
    if problems:
        for p in problems[:25]:
            log("model.json problem: %s" % p)
        raise BuildError("model.json failed validation with %d problem(s); nothing written" % len(problems))

    ledger.write_events(picks_path, out["new_snapshots"] + out["new_grades"])
    write_json_atomic(model, model_path)
    render.write_index(model, index_path, odds_url=odds_url)

    summary = {
        "build_id": model["build"]["id"],
        "built_at": built_at,
        "season": model["games"][0]["season"] if model["games"] else None,
        "games": len(model["games"]),
        "finals": sum(1 for g in model["games"] if g["status"] == "final"),
        "odds_source": model["build"]["odds_source"],
        "snapshots_written": len(out["new_snapshots"]),
        "grades_written": len(out["new_grades"]),
        "ledger_events": len(events) + len(out["new_snapshots"]) + len(out["new_grades"]),
        "fetched": report["fetched"],
        "seconds": time.perf_counter() - t0,
    }
    log("build %(build_id)s: %(games)d games (%(finals)d final), odds=%(odds_source)s, "
        "+%(snapshots_written)d snapshots +%(grades_written)d grades (%(ledger_events)d events), %(seconds).1f s" % summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    try:
        run()
    except (BuildError, data.DataError) as exc:
        print("build failed: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
