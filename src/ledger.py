"""Append-only pick ledger (picks.jsonl) and its track-record reducer. D10 / CONTRACT.md 4 + 6.6.

The ledger is a model pick ledger, not a bet log. Two event types, one JSON object per line:

* ``snapshot`` -- the official pick for a game as frozen by one daily build (CONTRACT 4.1).
* ``grade``    -- the outcome of ONE snapshot per game, written by the first build after the
                  game is final (CONTRACT 4.2).

Nothing is ever edited or deleted. A build appends new snapshot events only when the official
fields changed since the game's latest pre-kick snapshot (so reruns add nothing) and one grade
per game, for the latest snapshot whose ``build_at`` precedes kickoff. ``reduce`` folds the events
into the ``track_record`` object of CONTRACT 2.12 using frozen snapshot values only.

Injury reports (D18, CONTRACT 4.1 amendment 2026-10-13). A snapshot records the injury context
the pick was made in: ``injury_elo`` (``official.injuries.elo`` as used, 0 while the term is
disabled) and ``inj_home`` / ``inj_away`` (the starter counts, ``available``, ``report_filed`` and
``qb_listed`` of each side's report). DECISION on the write rule: these three keys are
INFORMATIONAL and are NOT in ``SNAPSHOT_OFFICIAL_FIELDS`` -- a report change alone never creates a
snapshot. Reasoning (CONTRACT 4.1): while the term is disabled a changed report changes nothing
official, and Wednesday-to-Friday report churn would otherwise add two or three snapshots per game
per week that grade identically; once the term is enabled a changed report moves ``p_model`` and
the existing rule fires on its own, so the graded snapshot always carries the counts it was priced
with. Readers tolerate events written before 2026-10-13 that lack the keys (``reduce`` files such
picks under the ``unknown`` injury-load bucket).

``reduce`` also computes ``by_injury_load`` (D18.5): the graded picks split by whether the PICKED
side had at least one starter listed Out at snapshot time (``starter_out`` / ``no_starter_out`` /
``unknown`` for legacy snapshots, unavailable or unfiled reports), each with n, hits, losses,
pushes, accuracy and market accuracy. It is always returned at the top level of ``reduce``'s
result and is published inside ``track_record`` only once ``schema.TRACK_RECORD_KEYS`` admits the
key (CONTRACT 2.12 does not list it yet and ``validate_model`` requires the exact key set).

Game dicts given to this module are the model.json ``games[]`` entries (CONTRACT 2.5) which the
build extends with the CSV close fields of the normalized game (CONTRACT 1):
``home_moneyline``, ``away_moneyline``, ``spread_line``, ``total_line`` (and optionally
``home_score``/``away_score``). Scores are read from ``result`` (2.7) first, then from those
top-level keys. The CSV closing moneylines fall back to the ``csv`` book inside
``official.market`` when the top-level keys are absent.

All math lives in pure functions; only ``read_events``/``write_events`` touch the filesystem.
Stdlib only, Python 3.11+.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .schema import INJURY_LOADS as _INJURY_LOADS, LEDGER_SCHEMA_VERSION, TIERS, TRACK_RECORD_KEYS

__all__ = [
    "SNAPSHOT_OFFICIAL_FIELDS",
    "SNAPSHOT_INJURY_FIELDS",
    "INJURY_SUMMARY_KEYS",
    "INJURY_LOADS",
    "injury_summary",
    "injury_load",
    "by_injury_load",
    "read_events",
    "write_events",
    "snapshot_event",
    "append_snapshots",
    "append_grades",
    "reduce",
    "build_snapshots",
    "build_grades",
    "append_events",
]

#: The official fields whose change triggers a new snapshot (CONTRACT 4.1 write rule).
#: The D18 injury keys (SNAPSHOT_INJURY_FIELDS) are deliberately absent: see the module docstring.
SNAPSHOT_OFFICIAL_FIELDS = (
    "pick", "p_model", "p_market", "p_blend", "tier", "pred_home", "pred_away",
    "best_price", "best_book", "ev", "market_source", "qb_home_id", "qb_away_id",
    "consensus_p_pick",
)
#: Informational snapshot keys recorded for the injury context (CONTRACT 4.1, D18).
SNAPSHOT_INJURY_FIELDS = ("injury_elo", "inj_home", "inj_away")
#: The per-side summary copied from official.injuries.home/away into a snapshot.
INJURY_SUMMARY_KEYS = ("available", "report_filed", "starters_out", "starters_doubtful", "starters_questionable", "qb_listed")
#: by_injury_load buckets, in output order.
INJURY_LOADS = _INJURY_LOADS  # ("starter_out", "no_starter_out", "unknown")

BREAK_EVEN_MINUS_110 = 110 / 210  # 0.5238...
CALIBRATION_BINS = 10
LOW_SAMPLE_N = 10


# --------------------------------------------------------------------------------------------
# Odds arithmetic (mirrors src/market.py formulas; kept local so the ledger has no dependency on
# a module under concurrent construction).
# --------------------------------------------------------------------------------------------

def _is_valid_american(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and abs(x) >= 100


def _decimal_profit(american: int) -> float:
    """Profit per 1 unit staked: +150 -> 1.5, -120 -> 0.8333..."""
    if not _is_valid_american(american):
        raise ValueError("American odds must be an int with |x| >= 100, got %r" % (american,))
    return american / 100 if american > 0 else 100 / -american


def _implied_prob(american: int) -> float:
    """Raw (vig-included) implied probability of an American price."""
    return 1 / (1 + _decimal_profit(american))


def _no_vig_pair(home_american: int, away_american: int) -> tuple[float, float]:
    """Normalize the two implied probabilities so they sum to 1 -> (p_home, p_away)."""
    h = _implied_prob(home_american)
    a = _implied_prob(away_american)
    s = h + a
    return h / s, a / s


def _break_even(payout: float) -> float:
    """Win rate needed to break even at a given profit-per-unit: 1/(1+payout)."""
    return 1 / (1 + payout)


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------

def _parse_utc(ts: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp (trailing Z) into an aware datetime."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _before(a: str, b: str) -> bool:
    """True when timestamp a is strictly earlier than timestamp b."""
    return _parse_utc(a) < _parse_utc(b)


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _game_season_week(game_id: str, game: dict | None) -> tuple[int | None, int | None]:
    """Season and week of a game, from the game dict or parsed from the nflverse game_id."""
    if game is not None and isinstance(game.get("season"), int) and isinstance(game.get("week"), int):
        return game["season"], game["week"]
    parts = game_id.split("_")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None, None


def _game_home(game_id: str, game: dict | None) -> str | None:
    if game is not None and isinstance(game.get("home"), str):
        return game["home"]
    parts = game_id.split("_")
    return parts[3] if len(parts) == 4 else None


def _game_scores(game: dict) -> tuple[int | None, int | None]:
    """(home_score, away_score) from result{} (CONTRACT 2.7) or the normalized-game keys."""
    res = game.get("result")
    if isinstance(res, dict):
        return res.get("home_score"), res.get("away_score")
    hs, as_ = game.get("home_score"), game.get("away_score")
    if isinstance(hs, int) and isinstance(as_, int):
        return hs, as_
    return None, None


def _is_final(game: dict) -> bool:
    if game.get("status") in ("final", "scheduled"):
        return game["status"] == "final"
    hs, as_ = _game_scores(game)
    return hs is not None and as_ is not None


def _csv_close_mls(game: dict) -> tuple[int | None, int | None]:
    """CSV closing moneylines (home, away); invalid or absent prices are None."""
    home_ml, away_ml = game.get("home_moneyline"), game.get("away_moneyline")
    if home_ml is None and away_ml is None:
        market = (game.get("official") or {}).get("market") or {}
        for book in market.get("books") or []:
            if book.get("key") == "csv":
                home_ml, away_ml = book.get("home_ml"), book.get("away_ml")
                break
    home_ml = home_ml if _is_valid_american(home_ml) else None
    away_ml = away_ml if _is_valid_american(away_ml) else None
    return home_ml, away_ml


def injury_summary(report) -> dict:
    """The CONTRACT 4.1 ``inj_home``/``inj_away`` summary of a 2.6b report.

    A missing or malformed report (an official built before D18) summarizes as unavailable."""
    rep_ = report if isinstance(report, dict) else {}
    available = bool(rep_.get("available"))
    return {
        "available": available,
        "report_filed": bool(rep_.get("report_filed")) if available else False,
        "starters_out": int(rep_.get("starters_out") or 0) if available else 0,
        "starters_doubtful": int(rep_.get("starters_doubtful") or 0) if available else 0,
        "starters_questionable": int(rep_.get("starters_questionable") or 0) if available else 0,
        "qb_listed": rep_.get("qb_listed") if available else None,
    }


def injury_load(summary) -> str:
    """D18.5 bucket of the picked side's snapshot summary: ``starter_out`` when at least one
    starter was listed Out, ``no_starter_out`` when a filed report listed none, else ``unknown``
    (no summary on a legacy snapshot, an unavailable report, or no report filed yet -- which is
    "no report", not "nobody hurt")."""
    if not isinstance(summary, dict) or not summary.get("available"):
        return "unknown"
    if int(summary.get("starters_out") or 0) >= 1:
        return "starter_out"
    if summary.get("report_filed"):
        return "no_starter_out"
    return "unknown"


def _pick_side(p_home, pick: str, home: str):
    """A home-side probability seen from the pick's side (None passes through)."""
    if p_home is None:
        return None
    return p_home if pick == home else 1 - p_home


# --------------------------------------------------------------------------------------------
# File I/O
# --------------------------------------------------------------------------------------------

def read_events(path: str) -> list[dict]:
    """Read every event of a picks.jsonl file in file order. A missing file is an empty ledger.

    Blank lines are skipped; a malformed line raises ValueError naming its line number (the
    ledger is append-only, so a bad line is a real problem, never something to paper over).
    """
    if not os.path.exists(path):
        return []
    events: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError("%s line %d: invalid JSON (%s)" % (path, lineno, exc)) from exc
            if not isinstance(event, dict) or "type" not in event:
                raise ValueError("%s line %d: event must be an object with a 'type'" % (path, lineno))
            events.append(event)
    return events


def write_events(path: str, new_events: list[dict]) -> None:
    """Append events to picks.jsonl, one JSON object per line. Existing bytes are never touched.

    If the file exists without a trailing newline, a newline is appended first so no existing
    line is ever extended. NaN/Infinity are rejected (``allow_nan=False``).
    """
    if not new_events:
        return
    needs_newline = False
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            needs_newline = fh.read(1) != b"\n"
    lines = [json.dumps(ev, ensure_ascii=False, allow_nan=False) + "\n" for ev in new_events]
    with open(path, "a", encoding="utf-8") as fh:
        if needs_newline:
            fh.write("\n")
        fh.write("".join(lines))


# --------------------------------------------------------------------------------------------
# Event construction
# --------------------------------------------------------------------------------------------

def _build_at(build: dict) -> str:
    return build.get("built_at") or build["build_at"]


def snapshot_event(game: dict, build: dict) -> dict | None:
    """The snapshot event (CONTRACT 4.1) for one scheduled game, or None.

    ``build`` is the model.json ``build`` object (``id``, ``built_at``). Returns None for finals
    and when ``built_at >= kickoff_utc`` (post-kick snapshots are never written).
    """
    if _is_final(game):
        return None
    build_at = _build_at(build)
    kickoff = game["kickoff_utc"]
    if not _before(build_at, kickoff):
        return None
    official = game["official"]
    home = game["home"]
    pick = official["pick"]
    market = official.get("market")
    best = official.get("best_price")
    factors = official.get("factors") or {}
    qb_home = factors.get("qb_home") or {}
    qb_away = factors.get("qb_away") or {}
    p_home_market = official.get("p_home_market")
    if p_home_market is None and market is not None:
        p_home_market = market.get("consensus_p_home")
    inj = official.get("injuries") if isinstance(official.get("injuries"), dict) else {}
    return {
        "type": "snapshot",
        "schema_version": LEDGER_SCHEMA_VERSION,
        "id": "%s@%s" % (game["game_id"], build["id"]),
        "game_id": game["game_id"],
        "kickoff_utc": kickoff,
        "build_at": build_at,
        "pick": pick,
        "p_model": _pick_side(official["p_home_model"], pick, home),
        "p_market": _pick_side(official.get("p_home_market"), pick, home),
        "p_blend": official["p_pick_blend"],
        "tier": official["tier"],
        "pred_home": official["display_home"],
        "pred_away": official["display_away"],
        "best_price": best["american"] if best else None,
        "best_book": best["book"] if best else None,
        "ev": official.get("estimated_ev_per_dollar"),
        "market_source": market.get("source") if market else None,
        "qb_home_id": qb_home.get("id"),
        "qb_away_id": qb_away.get("id"),
        "qb_home": qb_home.get("name"),
        "qb_away": qb_away.get("name"),
        "consensus_p_pick": _pick_side(p_home_market, pick, home),
        "total_used": official.get("total_used"),
        "total_source": official.get("total_source"),
        "injury_elo": float(inj.get("elo") or 0.0),
        "inj_home": injury_summary(inj.get("home")),
        "inj_away": injury_summary(inj.get("away")),
    }


def latest_prekick_snapshot(events: list[dict], game_id: str, kickoff_utc: str) -> dict | None:
    """Public: the snapshot a kicked-off game is graded on (latest with build_at < kickoff)."""
    return _latest_prekick(_snapshots_by_game(events).get(game_id, []), kickoff_utc)


def _snapshots_by_game(events: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for ev in events:
        if ev.get("type") == "snapshot" and isinstance(ev.get("game_id"), str):
            out.setdefault(ev["game_id"], []).append(ev)
    return out


def _latest_prekick(snapshots: list[dict], kickoff_utc: str) -> dict | None:
    """Latest snapshot with build_at < kickoff, ordered by (build_at, id). None if none."""
    eligible = [s for s in snapshots if _before(s["build_at"], kickoff_utc)]
    if not eligible:
        return None
    return max(eligible, key=lambda s: (_parse_utc(s["build_at"]), s["id"]))


def _official_fields_differ(a: dict, b: dict) -> bool:
    return any(a.get(k) != b.get(k) for k in SNAPSHOT_OFFICIAL_FIELDS)


def append_snapshots(events: list[dict], games: list[dict], build: dict) -> list[dict]:
    """NEW snapshot events this build should append (pure; nothing is written).

    One per scheduled pre-kick game whose official fields differ from the game's latest
    existing pre-kick snapshot (or that has none). Reruns of an identical build return [].
    """
    existing = _snapshots_by_game(events)
    new_events: list[dict] = []
    for game in games:
        candidate = snapshot_event(game, build)
        if candidate is None:
            continue
        latest = _latest_prekick(existing.get(game["game_id"], []), game["kickoff_utc"])
        if latest is None or _official_fields_differ(candidate, latest):
            new_events.append(candidate)
    return new_events


def _grade_for(snapshot: dict, game: dict, graded_at: str) -> dict:
    """The grade event (CONTRACT 4.2) of a final game against its selected snapshot."""
    home_score, away_score = _game_scores(game)
    margin = home_score - away_score
    home = game["home"]
    if margin > 0:
        result, winner = "home", home
    elif margin < 0:
        result, winner = "away", game["away"]
    else:
        result, winner = "tie", None
    pick = snapshot["pick"]
    hit = None if winner is None else (pick == winner)
    home_ml, away_ml = _csv_close_mls(game)
    close_p_pick = close_price_pick = None
    if home_ml is not None and away_ml is not None:
        p_home_close, _ = _no_vig_pair(home_ml, away_ml)
        close_p_pick = _pick_side(p_home_close, pick, home)
        close_price_pick = home_ml if pick == home else away_ml
    consensus = snapshot.get("consensus_p_pick")
    clv_pp = None if (close_p_pick is None or consensus is None) else 100 * (close_p_pick - consensus)
    return {
        "type": "grade",
        "schema_version": LEDGER_SCHEMA_VERSION,
        "snapshot_id": snapshot["id"],
        "game_id": game["game_id"],
        "graded_at": graded_at,
        "result": result,
        "margin": margin,
        "home_score": home_score,
        "away_score": away_score,
        "hit": hit,
        "close_p_pick": close_p_pick,
        "close_price_pick": close_price_pick,
        "clv_pp": clv_pp,
    }


def append_grades(events: list[dict], games: list[dict], graded_at: str) -> list[dict]:
    """NEW grade events (pure): one per final game not yet graded that has a pre-kick snapshot.

    The graded snapshot is the latest one with ``build_at < kickoff_utc`` (by ``(build_at, id)``);
    post-kick snapshots are ineligible. Ties grade as a push (``hit`` null).
    """
    graded = {ev["game_id"] for ev in events if ev.get("type") == "grade"}
    snapshots = _snapshots_by_game(events)
    new_events: list[dict] = []
    for game in games:
        gid = game["game_id"]
        if gid in graded or not _is_final(game):
            continue
        home_score, away_score = _game_scores(game)
        if home_score is None or away_score is None:
            continue
        snapshot = _latest_prekick(snapshots.get(gid, []), game["kickoff_utc"])
        if snapshot is None:
            continue
        new_events.append(_grade_for(snapshot, game, graded_at))
        graded.add(gid)
    return new_events


# --------------------------------------------------------------------------------------------
# Reducer
# --------------------------------------------------------------------------------------------

def _graded_rows(events: list[dict], games_by_id: dict[str, dict]) -> list[dict]:
    """Join each grade (first per game) with its snapshot and game context into one flat row."""
    snapshots = {ev["id"]: ev for ev in events if ev.get("type") == "snapshot"}
    rows: list[dict] = []
    seen: set[str] = set()
    for ev in events:
        if ev.get("type") != "grade" or ev["game_id"] in seen:
            continue
        snap = snapshots.get(ev.get("snapshot_id"))
        if snap is None:
            continue  # a grade without its snapshot cannot be aggregated
        seen.add(ev["game_id"])
        game = games_by_id.get(ev["game_id"])
        season, week = _game_season_week(ev["game_id"], game)
        home = _game_home(ev["game_id"], game)
        pick = snap["pick"]
        p_market = snap.get("p_market")
        market_pick_is_pick = None if (p_market is None or p_market == 0.5) else (p_market > 0.5)
        hit = ev["hit"]
        market_hit = None if (hit is None or market_pick_is_pick is None) else (hit == market_pick_is_pick)
        rows.append({
            "game_id": ev["game_id"],
            "season": season,
            "week": week,
            "tier": snap["tier"],
            "hit": hit,
            "market_hit": market_hit,
            "p_home_blend": _pick_side(snap["p_blend"], pick, home) if home else None,
            "home_win": None if hit is None else (ev["result"] == "home"),
            "pred_margin": snap["pred_home"] - snap["pred_away"],
            "pred_total": snap["pred_home"] + snap["pred_away"],
            "margin": ev["margin"],
            "total": ev["home_score"] + ev["away_score"],
            "spread_line": game.get("spread_line") if game else None,
            "total_line": game.get("total_line") if game else None,
            "clv_pp": ev.get("clv_pp"),
            "best_price": snap.get("best_price"),
            "ev": snap.get("ev"),
            "injury_load": injury_load(snap.get("inj_home" if pick == home else "inj_away") if home is not None else None),
            "grade": ev,
        })
    return rows


def _wlt(rows: list[dict]) -> dict:
    hits = sum(1 for r in rows if r["hit"] is True)
    losses = sum(1 for r in rows if r["hit"] is False)
    pushes = sum(1 for r in rows if r["hit"] is None)
    market_rows = [r for r in rows if r["market_hit"] is not None]
    market_hits = sum(1 for r in market_rows if r["market_hit"])
    return {
        "n": len(rows),
        "hits": hits,
        "losses": losses,
        "pushes": pushes,
        "accuracy": _ratio(hits, hits + losses),
        "market_n": len(market_rows),
        "market_hits": market_hits,
        "market_accuracy": _ratio(market_hits, len(market_rows)),
    }


def _by_tier(rows: list[dict]) -> list[dict]:
    out = []
    for tier in TIERS:
        agg = _wlt([r for r in rows if r["tier"] == tier])
        out.append({"tier": tier, **agg})
    return out


def by_injury_load(rows: list[dict]) -> list[dict]:
    """D18.5: the graded rows split by the picked side's injury load at snapshot time, one
    entry per INJURY_LOADS bucket (always all three, n on each) with the _wlt aggregate."""
    out = []
    for load in INJURY_LOADS:
        agg = _wlt([r for r in rows if r.get("injury_load", "unknown") == load])
        out.append({"load": load, **agg})
    return out


def _by_week(rows: list[dict]) -> list[dict]:
    keys = sorted({(r["season"], r["week"]) for r in rows if r["season"] is not None})
    out = []
    cum = {}
    for season, week in keys:
        agg = _wlt([r for r in rows if (r["season"], r["week"]) == (season, week)])
        c = cum.setdefault(season, {"n": 0, "hits": 0, "losses": 0, "mn": 0, "mh": 0})
        c["n"] += agg["n"]
        c["hits"] += agg["hits"]
        c["losses"] += agg["losses"]
        c["mn"] += agg["market_n"]
        c["mh"] += agg["market_hits"]
        out.append({
            "season": season,
            "week": week,
            "n": agg["n"],
            "hits": agg["hits"],
            "losses": agg["losses"],
            "pushes": agg["pushes"],
            "cum_n": c["n"],
            "cum_hits": c["hits"],
            "cum_accuracy": _ratio(c["hits"], c["hits"] + c["losses"]),
            "market_n": agg["market_n"],
            "market_hits": agg["market_hits"],
            "market_cum_n": c["mn"],
            "market_cum_hits": c["mh"],
            "market_cum_accuracy": _ratio(c["mh"], c["mn"]),
        })
    return out


def _bin_index(p: float) -> int:
    """Bin i covers [i/10, (i+1)/10); the last bin is closed at 1.0."""
    for i in range(CALIBRATION_BINS - 1):
        if p < (i + 1) / CALIBRATION_BINS:
            return i
    return CALIBRATION_BINS - 1


def _calibration(rows: list[dict]) -> dict:
    eligible = [r for r in rows if r["home_win"] is not None and r["p_home_blend"] is not None]
    buckets: list[list[dict]] = [[] for _ in range(CALIBRATION_BINS)]
    for r in eligible:
        buckets[_bin_index(r["p_home_blend"])].append(r)
    bins = []
    err_sum = 0.0
    err_n = 0
    for i, bucket in enumerate(buckets):
        n = len(bucket)
        mean_p = _mean([r["p_home_blend"] for r in bucket])
        hit_rate = _ratio(sum(1 for r in bucket if r["home_win"]), n)
        low_sample = n < LOW_SAMPLE_N
        if not low_sample:
            err_sum += n * abs(mean_p - hit_rate)
            err_n += n
        bins.append({
            "lo": i / CALIBRATION_BINS,
            "hi": (i + 1) / CALIBRATION_BINS,
            "n": n,
            "mean_p": mean_p,
            "hit_rate": hit_rate,
            "low_sample": low_sample,
        })
    return {
        "n": len(eligible),
        "bins": bins,
        "error": err_sum / err_n if err_n else None,
        "error_n": err_n,
    }


def _score_error(rows: list[dict]) -> dict:
    """MAE of predicted margin/total vs actual; market = CSV closing spread_line/total_line.

    The model side uses the DISPLAYED integer scores frozen in the snapshot (``pred_home`` /
    ``pred_away``, CONTRACT 4.1 carries nothing else), so the D5 favorite nudge can add up to
    1 point to a predicted margin; CONTRACT 2.12 states this."""
    market_rows = [r for r in rows if r["spread_line"] is not None and r["total_line"] is not None]
    return {
        "n": len(rows),
        "margin_mae": _mean([abs(r["pred_margin"] - r["margin"]) for r in rows]),
        "total_mae": _mean([abs(r["pred_total"] - r["total"]) for r in rows]),
        "market_n": len(market_rows),
        "market_margin_mae": _mean([abs(r["spread_line"] - r["margin"]) for r in market_rows]),
        "market_total_mae": _mean([abs(r["total_line"] - r["total"]) for r in market_rows]),
    }


def _clv(rows: list[dict]) -> dict:
    vals = [r["clv_pp"] for r in rows if r["clv_pp"] is not None]
    return {
        "n": len(vals),
        "mean_pp": _mean(vals),
        "share_toward_pick": _ratio(sum(1 for v in vals if v > 0), len(vals)),
    }


def _units_block(rows: list[dict]) -> dict:
    """1 unit per priced pick at best_price: hit +payout, loss -1, push 0."""
    priced = [r for r in rows if _is_valid_american(r["best_price"])]
    units = 0.0
    break_evens = []
    for r in priced:
        payout = _decimal_profit(r["best_price"])
        break_evens.append(_break_even(payout))
        if r["hit"] is True:
            units += payout
        elif r["hit"] is False:
            units -= 1
    n = len(priced)
    return {
        "n": n,
        "hits": sum(1 for r in priced if r["hit"] is True),
        "losses": sum(1 for r in priced if r["hit"] is False),
        "pushes": sum(1 for r in priced if r["hit"] is None),
        "units": units,
        "roi": _ratio(units, n),
        "mean_break_even": _mean(break_evens),
    }


def _units(rows: list[dict]) -> dict:
    return {
        "all": _units_block(rows),
        "ev_positive": _units_block([r for r in rows if r["ev"] is not None and r["ev"] >= 0]),
        "break_even_minus_110": BREAK_EVEN_MINUS_110,
    }


def _current_season(games_by_id: dict[str, dict], rows: list[dict]) -> int | None:
    seasons = [g["season"] for g in games_by_id.values() if isinstance(g.get("season"), int)]
    if not seasons:
        seasons = [r["season"] for r in rows if r["season"] is not None]
    return max(seasons) if seasons else None


def reduce(events: list[dict], games: list[dict] | dict[str, dict], params: dict | None = None) -> dict:
    """Fold ledger events into the track record (CONTRACT 2.12) from frozen snapshot values.

    ``games`` supplies season/week, the home code, and the CSV close lines (``spread_line``,
    ``total_line``) per game; it may be a list or a ``{game_id: game}`` dict. ``params`` is
    accepted for signature compatibility and unused (every input is already frozen in the
    events). Returns ``{"track_record", "scorecard_season", "grades": {game_id: grade},
    "by_injury_load"}`` (the D18.5 split; see the module docstring for when it is also
    inside ``track_record``).
    """
    games_by_id = games if isinstance(games, dict) else {g["game_id"]: g for g in games}
    rows = _graded_rows(events, games_by_id)
    overall = _wlt(rows)
    season = _current_season(games_by_id, rows)
    season_agg = _wlt([r for r in rows if r["season"] == season]) if season is not None else _wlt([])
    injury_split = by_injury_load(rows)
    track_record = {
        "overall": overall,
        "by_tier": _by_tier(rows),
        "by_week": _by_week(rows),
        "calibration": _calibration(rows),
        "score_error": _score_error(rows),
        "clv": _clv(rows),
        "units": _units(rows),
    }
    if "by_injury_load" in TRACK_RECORD_KEYS:  # published once CONTRACT 2.12 / the schema admit it
        track_record["by_injury_load"] = injury_split
    scorecard_season = {
        "season": season,
        "n": season_agg["n"],
        "hits": season_agg["hits"],
        "losses": season_agg["losses"],
        "pushes": season_agg["pushes"],
        "accuracy": season_agg["accuracy"],
        "market_n": season_agg["market_n"],
        "market_accuracy": season_agg["market_accuracy"],
    }
    return {
        "track_record": track_record,
        "scorecard_season": scorecard_season,
        "grades": {r["game_id"]: r["grade"] for r in rows},
        "by_injury_load": injury_split,
    }


# --------------------------------------------------------------------------------------------
# Aliases under the names used in the build brief (same behaviour as the CONTRACT 6.6 surface)
# --------------------------------------------------------------------------------------------

def build_snapshots(games_official: list[dict], build_id: str, build_at: str, existing_events: list[dict]) -> list[dict]:
    """Alias of ``append_snapshots`` taking the build id and timestamp separately."""
    return append_snapshots(existing_events, games_official, {"id": build_id, "built_at": build_at})


def build_grades(games_with_results: list[dict], existing_events: list[dict], graded_at: str) -> list[dict]:
    """Alias of ``append_grades`` with the argument order used in the build brief."""
    return append_grades(existing_events, games_with_results, graded_at)


def append_events(path: str, events: list[dict]) -> None:
    """Alias of ``write_events``."""
    write_events(path, events)
