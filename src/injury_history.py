"""Historical injury features for the D18.6 tuning stage (nflverse 2010-2025).

    fetch_history(seasons, cache_dir, offline=False, timeout=120, log=None) -> {season: {"injuries", "depth_path", "notes"}}
    load_depth_rows(path, season, games)        -> (rows in injuries.DEPTH_COLUMNS shape, "dt" | "legacy")
    season_features(games, season, injury_rows, depth_rows) -> {"games": {game_id: {"home": side, "away": side}}, "coverage": {...}}
    build_history(games, seasons, cache_dir, offline=False, timeout=120, log=None) -> {"features", "coverage", "notes"}
    side_term(side, weights, status_mult)       -> float <= 0   (the D18.6 formula on a feature row)
    extra_from_features(features, weights, status_mult) -> {game_id: (home_term, away_term)}  (elo.replay's hook)
    as_of_for_gameday(gameday)                  -> ISO UTC: the day before gameday, 23:59:59Z

Per historical game the per-team report is built with injuries.report_for and
injuries.starters_by_team exactly as the build does (starter = pos_rank "1" on the latest
chart before gameday, offence/defence only, QB excluded through covered_by_qb_term), and
reduced to one feature row per side: counts[pos_group][status] over counted starters.

Two depth-chart formats exist on nflverse. 2025+ files carry timestamped snapshots
(dt, team, pos_grp, pos_abb, pos_rank). 2010-2024 files are the legacy weekly charts
(season, club_code, week, formation, depth_team, depth_position, position). Legacy rows are
mapped onto the new shape: dt = the day before the team's game that week (00:00:00Z), so
"latest chart before gameday" selects that week's chart; formation -> pos_grp; depth_team
-> pos_rank; depth_position -> pos_abb through LEGACY_SLOT_ALIAS (RE -> DE, WILL -> WLB, ...),
falling back to the roster position when the slot name is not a recognised position.

Downloads (public GitHub release assets) stream to a temp file and are atomically renamed;
a cached season is reused as-is (history is immutable; delete the file to refresh). A season
whose files cannot be obtained is skipped with a logged note, never an exception.
Stdlib only.
"""
from __future__ import annotations

import csv
import os
import shutil
import sys
import tempfile
import urllib.request
from datetime import date, timedelta

from . import injuries, schema

HISTORY_SEASONS = tuple(range(2010, 2026))
STATUS_MULT = {"Out": 1.0, "Doubtful": 0.75, "Questionable": 0.25, "Probable": 0.1}
WEIGHT_GROUPS = schema.INJURY_WEIGHT_GROUPS
STATUSES = schema.INJURY_STATUSES
LEGACY_DEPTH_COLUMNS = ("season", "club_code", "week", "game_type", "depth_team", "formation",
                        "gsis_id", "position", "depth_position", "full_name")
DEPTH_COLUMNS = injuries.DEPTH_COLUMNS

# Legacy depth_position slot names that injuries.pos_group does not know, mapped to a known code.
LEGACY_SLOT_ALIAS = {
    "RE": "DE", "LE": "DE", "END": "DE", "RUSH": "EDGE", "LEO": "EDGE", "JACK": "OLB", "OTTO": "OLB",
    "WILL": "WLB", "MIKE": "MLB", "SAM": "SLB", "MILB": "ILB", "MIL": "ILB", "BLB": "LB", "JLB": "LB",
    "TED": "LB", "MOLB": "OLB", "$LB": "LB", "UT": "DT", "WS": "S", "MCB": "CB", "NCB": "NB", "NKL": "NB",
    "NICKE": "NB", "N": "NB", "NDB": "DB", "DL/OL": "DL", "ROT": "RT", "LOT": "LT", "OC": "C",
    "HB-TE": "TE", "H-B": "TE", "KOR": "KR",
}


def _log(msg: str, log=None) -> None:
    print("injury_history: " + msg, file=sys.stderr)
    if log:
        log("injury_history: " + msg)


def as_of_for_gameday(gameday: str) -> str:
    """The depth-chart / report cutoff for a game: the day before gameday, end of day (UTC)."""
    return (date.fromisoformat(gameday) - timedelta(days=1)).isoformat() + "T23:59:59Z"


def legacy_dt_for_gameday(gameday: str) -> str:
    """Synthetic snapshot time for a legacy weekly chart: the day before gameday, 00:00:00Z."""
    return (date.fromisoformat(gameday) - timedelta(days=1)).isoformat() + "T00:00:00Z"


# ----------------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------------


def depth_chart_format(path: str) -> str | None:
    """"dt" (2025+ snapshots), "legacy" (2010-2024 weekly charts) or None (unusable)."""
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), [])
    except (OSError, UnicodeDecodeError, csv.Error, StopIteration):
        return None
    cols = set(c.strip() for c in header)
    if all(c in cols for c in DEPTH_COLUMNS):
        return "dt"
    if all(c in cols for c in LEGACY_DEPTH_COLUMNS):
        return "legacy"
    return None


def _download(url: str, dest: str, timeout: int) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "gridiron-odds/1.0 (stdlib urllib)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)  # stream: the 2025+ files are ~50 MB


def fetch_depth_chart_file(season: int, cache_dir: str, offline: bool = False, timeout: int = 120,
                           _downloader=None, log=None) -> str | None:
    """Path of a usable depth_charts_{season}.csv (either format) or None; never raises."""
    path = os.path.join(cache_dir, "depth_charts_%d.csv" % season)
    if os.path.exists(path):
        if depth_chart_format(path):
            return path
        _log("depth_charts_%d: cache at %s is not a depth-chart file" % (season, path), log)
        if offline:
            return None
    elif offline:
        _log("depth_charts_%d: offline and no cache at %s -> skipped" % (season, path), log)
        return None
    url = injuries.DEPTH_CHARTS_URL.format(season=season)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="depth_charts_%d.csv." % season, suffix=".tmp", dir=cache_dir)
        os.close(fd)
    except OSError as exc:
        _log("depth_charts_%d: cannot create a temp file (%s) -> skipped" % (season, exc), log)
        return None
    try:
        try:
            (_downloader or _download)(url, tmp, timeout)
        except Exception as exc:  # network, HTTP, timeout
            _log("depth_charts_%d: download of %s failed (%s) -> skipped" % (season, url, exc), log)
            return None
        if depth_chart_format(tmp) is None:
            _log("depth_charts_%d: downloaded file is not a depth-chart CSV -> skipped" % season, log)
            return None
        try:
            os.replace(tmp, path)
        except OSError as exc:
            _log("depth_charts_%d: cannot write the cache (%s) -> skipped" % (season, exc), log)
            return None
        return path
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def fetch_history(seasons, cache_dir: str, offline: bool = False, timeout: int = 120, log=None,
                  _downloader=None) -> dict:
    """{season: {"injuries": rows | None, "depth_path": path | None, "notes": [str]}} per season.

    Cached files are reused without re-downloading (history does not change); only missing
    files are fetched, and never when ``offline``."""
    out = {}
    for season in seasons:
        notes = []
        inj_path = os.path.join(cache_dir, "injuries_%d.csv" % season)
        have = os.path.exists(inj_path)
        inj_rows = injuries.fetch_injuries(season, cache_dir, offline=(offline or have), timeout=timeout,
                                           _downloader=_downloader)
        if inj_rows is None:
            notes.append("injuries_%d.csv unavailable" % season)
        depth_path = fetch_depth_chart_file(season, cache_dir, offline=offline, timeout=timeout,
                                            _downloader=_downloader, log=log)
        if depth_path is None:
            notes.append("depth_charts_%d.csv unavailable" % season)
        out[season] = {"injuries": inj_rows, "depth_path": depth_path, "notes": notes}
    return out


# ----------------------------------------------------------------------------
# Depth-chart rows (both formats -> injuries.DEPTH_COLUMNS shape)
# ----------------------------------------------------------------------------


def _clean_slot(slot: str | None) -> str:
    return "".join((slot or "").split()).upper()


def legacy_pos_abb(depth_position: str | None, position: str | None) -> str:
    """Resolve a legacy slot name to a code injuries.pos_group understands.

    Alias table first (RE -> DE, WILL -> WLB, ...); when the slot is still unknown (a formation
    nickname, blank, or a typo) the roster position decides. Kicking slots stay as they are so
    starters_by_team keeps excluding them."""
    slot = _clean_slot(depth_position)
    abb = LEGACY_SLOT_ALIAS.get(slot, slot)
    if abb in injuries.ST_POSITIONS:
        return abb
    if injuries.pos_group(abb) != "ST":
        return abb
    roster = _clean_slot(position)
    roster = LEGACY_SLOT_ALIAS.get(roster, roster)
    if roster and (roster in injuries.ST_POSITIONS or injuries.pos_group(roster) != "ST"):
        return roster
    return abb or roster


def gameday_index(games: list[dict]) -> dict:
    """{(season, team, week): gameday} over every game (played or not)."""
    idx = {}
    for g in games:
        for side in ("home", "away"):
            idx[(g["season"], g[side], g["week"])] = g["gameday"]
    return idx


def legacy_to_dt_rows(legacy_rows: list[dict], games: list[dict]) -> tuple[list[dict], dict]:
    """Map legacy weekly rows to the DEPTH_COLUMNS shape. Returns (rows, stats).

    dt = the day before the team's game that week; rows for a (season, team, week) with no game
    in ``games`` (a bye, a missing week) are dropped and counted in stats["dropped_no_game"]."""
    idx = gameday_index(games)
    out = []
    dropped = 0
    unmapped: dict = {}
    for r in legacy_rows:
        team = schema.canonical_code((r.get("club_code") or "").strip())
        try:
            season = int(r.get("season") or 0)
            week = int(r.get("week") or 0)
        except ValueError:
            dropped += 1
            continue
        gameday = idx.get((season, team, week)) if team else None
        if gameday is None:
            dropped += 1
            continue
        abb = legacy_pos_abb(r.get("depth_position"), r.get("position"))
        formation = (r.get("formation") or "").strip()
        if (injuries.pos_group(abb) == "ST" and abb not in injuries.ST_POSITIONS
                and (r.get("depth_team") or "").strip() == "1" and injuries.unit_of(formation) != "ST"):
            key = _clean_slot(r.get("depth_position")) or "(blank)"
            unmapped[key] = unmapped.get(key, 0) + 1
        out.append({
            "dt": legacy_dt_for_gameday(gameday),
            "team": team,
            "player_name": (r.get("full_name") or "").strip(),
            "gsis_id": (r.get("gsis_id") or "").strip(),
            "pos_grp": formation,
            "pos_abb": abb,
            "pos_rank": (r.get("depth_team") or "").strip(),
        })
    return out, {"dropped_no_game": dropped, "unmapped_starter_slots": unmapped}


def load_depth_rows(path: str, season: int, games: list[dict]) -> tuple[list[dict], str, dict]:
    """(rows in DEPTH_COLUMNS shape, format, stats) for one season's cached file. Raises ValueError."""
    fmt = depth_chart_format(path)
    if fmt == "dt":
        rows = injuries._read_rows(path, DEPTH_COLUMNS, None)
        unmapped: dict = {}
        for r in rows:
            abb = (r.get("pos_abb") or "").strip().upper()
            if (r.get("pos_rank") == "1" and injuries.unit_of(r.get("pos_grp")) != "ST"
                    and abb not in injuries.ST_POSITIONS and injuries.pos_group(abb) == "ST"):
                unmapped[abb or "(blank)"] = unmapped.get(abb or "(blank)", 0) + 1
        return rows, fmt, {"dropped_no_game": 0, "unmapped_starter_slots": unmapped}
    if fmt == "legacy":
        legacy = injuries._read_rows(path, LEGACY_DEPTH_COLUMNS, season)
        rows, stats = legacy_to_dt_rows(legacy, games)
        return rows, fmt, stats
    raise ValueError("%s is not a depth-chart file" % path)


# ----------------------------------------------------------------------------
# Features
# ----------------------------------------------------------------------------


def empty_side() -> dict:
    return {"report_filed": False, "starters": 0, "counts": {g: {s: 0 for s in STATUSES} for g in WEIGHT_GROUPS}}


def side_from_report(report: dict, n_starters: int) -> dict:
    """One feature row from a 2.6b report: counts[pos_group][status] over counted starters."""
    side = empty_side()
    side["report_filed"] = bool(report.get("report_filed"))
    side["starters"] = int(n_starters)
    for p in report.get("players") or []:
        if not p.get("starter") or p.get("covered_by_qb_term"):
            continue
        grp, st = p.get("pos_group"), p.get("status")
        if grp in side["counts"] and st in side["counts"][grp]:
            side["counts"][grp][st] += 1
    return side


def side_term(side: dict, weights: dict, status_mult: dict = STATUS_MULT) -> float:
    """-(sum weights[group] * status_mult[status] * count) over a feature row; 0 when nothing counts."""
    total = 0.0
    for grp, by_status in side["counts"].items():
        w = float(weights.get(grp, 0.0))
        if not w:
            continue
        for st, n in by_status.items():
            if n:
                total += w * float(status_mult.get(st, 0.0)) * n
    return -total if total else 0.0


def extra_from_features(features: dict, weights: dict, status_mult: dict = STATUS_MULT) -> dict:
    """elo.replay's ``extra`` map: {game_id: (home_term, away_term)} for every game with features."""
    return {gid: (side_term(f["home"], weights, status_mult), side_term(f["away"], weights, status_mult))
            for gid, f in features.items()}


def season_features(games: list[dict], season: int, injury_rows: list[dict], depth_rows: list[dict]) -> dict:
    """Feature rows for every played game of ``season``.

    Returns {"games": {game_id: {"home": side, "away": side}}, "coverage": {...}}. Each side's
    report is injuries.report_for over that team-week's rows with starters from
    injuries.starters_by_team on that team's rows at as_of_for_gameday(gameday)."""
    inj_idx: dict = {}
    for r in injury_rows:
        if (r.get("season") or "").strip() != str(season):
            continue
        team = schema.canonical_code((r.get("team") or "").strip())
        if team is None:
            continue
        inj_idx.setdefault((team, (r.get("week") or "").strip()), []).append(r)
    depth_idx: dict = {}
    for r in depth_rows:
        team = schema.canonical_code((r.get("team") or "").strip())
        if team is not None:
            depth_idx.setdefault(team, []).append(r)

    out: dict = {}
    cov = {"games": 0, "sides_report_filed": 0, "sides_with_starters": 0, "games_both_filed": 0,
           "starters_out": 0, "starters_doubtful": 0, "starters_questionable": 0, "starters_probable": 0,
           "starters_per_side_sum": 0}
    starters_cache: dict = {}
    for g in games:
        if g["season"] != season or not g["played"]:
            continue
        as_of = as_of_for_gameday(g["gameday"])
        entry = {}
        for side in ("home", "away"):
            team = g[side]
            key = (team, as_of)
            if key not in starters_cache:
                starters_cache[key] = injuries.starters_by_team(depth_idx.get(team, []), as_of)
            starters = starters_cache[key]
            n_starters = len(starters.get(team, {}))
            report = injuries.report_for(inj_idx.get((team, str(g["week"])), []), starters, team, season,
                                         g["week"], g[side + "_qb_id"])
            row = side_from_report(report, n_starters)
            entry[side] = row
            cov["sides_report_filed"] += 1 if row["report_filed"] else 0
            cov["sides_with_starters"] += 1 if n_starters else 0
            cov["starters_per_side_sum"] += n_starters
            cov["starters_out"] += report["starters_out"]
            cov["starters_doubtful"] += report["starters_doubtful"]
            cov["starters_questionable"] += report["starters_questionable"]
            cov["starters_probable"] += sum(row["counts"][grp]["Probable"] for grp in WEIGHT_GROUPS)
        out[g["game_id"]] = entry
        cov["games"] += 1
        if entry["home"]["report_filed"] and entry["away"]["report_filed"]:
            cov["games_both_filed"] += 1
    return {"games": out, "coverage": cov}


def build_history(games: list[dict], seasons=HISTORY_SEASONS, cache_dir: str | None = None,
                  offline: bool = False, timeout: int = 120, log=None, _downloader=None) -> dict:
    """Fetch (or reuse) every season's files and compute the feature rows.

    Returns {"features": {game_id: {"home", "away"}}, "coverage": {season: {...}}, "notes": [str]}.
    A season with a missing file is skipped (a note is recorded and logged); its games get no
    features and therefore a 0 term."""
    cache_dir = cache_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    fetched = fetch_history(seasons, cache_dir, offline=offline, timeout=timeout, log=log, _downloader=_downloader)
    features: dict = {}
    coverage: dict = {}
    notes: list = []
    for season in seasons:
        f = fetched[season]
        if f["injuries"] is None or f["depth_path"] is None:
            msg = "season %d skipped: %s" % (season, "; ".join(f["notes"]))
            notes.append(msg)
            _log(msg, log)
            coverage[season] = {"skipped": True, "notes": list(f["notes"])}
            continue
        try:
            depth_rows, fmt, stats = load_depth_rows(f["depth_path"], season, games)
        except (OSError, ValueError, UnicodeDecodeError, csv.Error) as exc:
            msg = "season %d skipped: depth chart unusable (%s)" % (season, exc)
            notes.append(msg)
            _log(msg, log)
            coverage[season] = {"skipped": True, "notes": [msg]}
            continue
        sf = season_features(games, season, f["injuries"], depth_rows)
        features.update(sf["games"])
        cov = dict(sf["coverage"])
        cov.update({"skipped": False, "depth_format": fmt, "injury_rows": len(f["injuries"]), "depth_rows": len(depth_rows),
                    "depth_dropped_no_game": stats["dropped_no_game"],
                    "unmapped_starter_slots": dict(sorted(stats["unmapped_starter_slots"].items(), key=lambda kv: -kv[1])[:12])})
        coverage[season] = cov
        if log:
            log("injury_history: %d %-6s games=%d both_filed=%d starters/side=%.1f out=%d" % (
                season, fmt, cov["games"], cov["games_both_filed"],
                cov["starters_per_side_sum"] / max(1, 2 * cov["games"]), cov["starters_out"]))
        del depth_rows
    return {"features": features, "coverage": coverage, "notes": notes}
