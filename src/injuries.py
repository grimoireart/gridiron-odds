"""Injury reports (D18): nflverse injuries + depth charts -> the CONTRACT 2.6b object.

Public surface (docs/CONTRACT.md section 6.7):
    fetch_injuries(season, cache_dir, offline=False, timeout=60) -> rows | None
    fetch_depth_charts(season, cache_dir, offline=False, timeout=60) -> rows | None
    starters_by_team(depth_rows, as_of_iso) -> {team: {gsis_id: {name, pos_abb, pos_grp}}}
    report_for(injury_rows, starters, team, season, week, listed_qb_id) -> report (2.6b)
    unavailable_report() -> report with available: false
    pos_group(position) -> "OL"|"WR_TE"|"RB"|"DL_EDGE"|"LB"|"DB"|"QB"|"ST"
    injury_elo(report, params) -> float            D18.6 (<= 0; 0 when disabled or unavailable)
    injuries_block(home_report, away_report, params, as_of_iso) -> official.injuries (2.6b)

Stdlib only. The two fetch_* functions follow D12 (temp download -> validate -> atomic
replace) but DEGRADE instead of failing: on any problem they log one line to stderr and
return the existing cache, or None when no usable cache exists. Everything else is pure.
Player names and injury strings are untrusted feed text and stay plain strings.
"""
from __future__ import annotations

import csv
import os
import shutil
import sys
import tempfile
import urllib.request

from . import schema

INJURIES_URL = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv"
DEPTH_CHARTS_URL = "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_{season}.csv"

INJURY_COLUMNS = ("season", "week", "team", "gsis_id", "position", "full_name", "report_status", "report_primary_injury")
DEPTH_COLUMNS = ("dt", "team", "player_name", "gsis_id", "pos_grp", "pos_abb", "pos_rank")
ROW_COUNT_SLACK = 1000  # D12: a refresh must not shrink the cache by more than this

STATUSES = schema.INJURY_STATUSES  # severity order; Probable only before 2016
STATUS_RANK = {s: i for i, s in enumerate(STATUSES)}
GROUPS = schema.INJURY_GROUPS
WEIGHT_GROUPS = schema.INJURY_WEIGHT_GROUPS  # params.injury.weights keys
ST_POSITIONS = frozenset(["K", "PK", "P", "LS", "KR", "PR", "H"])

_POS_TO_GROUP = {}
for _g, _codes in (
    ("OL", ("T", "OT", "LT", "RT", "G", "OG", "LG", "RG", "C", "OL")),
    ("WR_TE", ("WR", "TE")),
    ("RB", ("RB", "HB", "FB")),
    ("DL_EDGE", ("DE", "DT", "NT", "DL", "EDGE", "LDE", "RDE", "LDT", "RDT", "DI")),
    ("LB", ("LB", "ILB", "OLB", "MLB", "WLB", "SLB", "LILB", "RILB", "LOLB", "ROLB")),
    ("DB", ("CB", "S", "FS", "SS", "DB", "NB", "SAF", "LCB", "RCB")),
    ("QB", ("QB",)),
    ("ST", tuple(sorted(ST_POSITIONS))),
):
    for _c in _codes:
        _POS_TO_GROUP[_c] = _g

REPORT_KEYS = schema.INJURY_REPORT_KEYS
PLAYER_KEYS = schema.INJURY_PLAYER_KEYS
BLOCK_KEYS = schema.INJURIES_KEYS


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------


def _s(v) -> str | None:
    """Trimmed string or None (never '')."""
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _log(msg: str) -> None:
    print("injuries: " + msg, file=sys.stderr)


def pos_group(position: str | None) -> str:
    """Map a report position or depth-chart slot (T, LCB, WLB, ...) to a model group.

    Unknown or blank positions map to "ST", the no-weight bucket."""
    p = _s(position)
    if p is None:
        return "ST"
    return _POS_TO_GROUP.get(p.upper(), "ST")


def unit_of(pos_grp: str | None) -> str:
    """Depth-chart unit for a formation group name: "O", "D" or "ST"."""
    g = (_s(pos_grp) or "").lower()
    if "special" in g:
        return "ST"
    if g.endswith(" d") or "defense" in g or g == "d":
        return "D"
    return "O"


# ----------------------------------------------------------------------------
# Fetch (D12 style, degrading)
# ----------------------------------------------------------------------------


def _download(url: str, dest: str, timeout: int) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "gridiron-odds/1.0 (stdlib urllib)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _read_rows(path: str, columns: tuple, season: int | None) -> list[dict]:
    """Read a CSV keeping `columns`; raise ValueError when it is not a usable file."""
    with open(path, "rb") as fh:
        head = fh.read(512)
    if not head:
        raise ValueError("file is empty")
    if head.lstrip()[:1] in (b"<", b"{"):
        raise ValueError("not a CSV (starts with %r)" % head.lstrip()[:15].decode("latin-1"))
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        missing = [c for c in columns if c not in cols]
        if missing:
            raise ValueError("missing columns %s" % missing)
        for r in reader:
            rows.append({c: (r.get(c) or "") for c in columns})
    if not rows:
        raise ValueError("no rows")
    if season is not None and "season" in columns:
        bad = sum(1 for r in rows if _s(r["season"]) != str(season))
        if bad:
            raise ValueError("%d rows are not season %d" % (bad, season))
    return rows


def _fetch(kind: str, url: str, cache_path: str, columns: tuple, season: int | None,
           offline: bool, timeout: int, _downloader=None) -> list[dict] | None:
    have_cache = os.path.exists(cache_path)

    def from_cache(reason: str) -> list[dict] | None:
        if not have_cache:
            _log("%s: %s; no cache at %s -> unavailable" % (kind, reason, cache_path))
            return None
        try:
            rows = _read_rows(cache_path, columns, season)
        except (OSError, ValueError, UnicodeDecodeError, csv.Error) as exc:
            _log("%s: %s; cache %s unusable (%s) -> unavailable" % (kind, reason, cache_path, exc))
            return None
        if reason != "offline":
            _log("%s: %s; using the cache (%d rows)" % (kind, reason, len(rows)))
        return rows

    if offline:
        return from_cache("offline")
    d = os.path.dirname(os.path.abspath(cache_path)) or "."
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(cache_path) + ".", suffix=".tmp", dir=d)
        os.close(fd)
    except OSError as exc:
        return from_cache("cannot create a temp file (%s)" % exc)
    try:
        try:
            (_downloader or _download)(url, tmp, timeout)
        except Exception as exc:  # network, HTTP, timeout
            return from_cache("download of %s failed (%s)" % (url, exc))
        try:
            rows = _read_rows(tmp, columns, season)
        except (OSError, ValueError, UnicodeDecodeError, csv.Error) as exc:
            return from_cache("downloaded file failed validation (%s)" % exc)
        if have_cache:
            try:
                with open(cache_path, newline="", encoding="utf-8") as fh:
                    cached_n = sum(1 for _ in fh) - 1
            except OSError:
                cached_n = 0
            if len(rows) < cached_n - ROW_COUNT_SLACK:
                return from_cache("downloaded file has %d rows, cache has %d" % (len(rows), cached_n))
        try:
            os.replace(tmp, cache_path)  # atomic on POSIX
        except OSError as exc:
            return from_cache("cannot replace the cache (%s)" % exc)
        return rows
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def fetch_injuries(season: int, cache_dir: str, offline: bool = False, timeout: int = 60,
                   _downloader=None) -> list[dict] | None:
    """Rows of nflverse injuries_{season}.csv (INJURY_COLUMNS only), or None when unavailable."""
    return _fetch("injuries_%d" % season, INJURIES_URL.format(season=season),
                  os.path.join(cache_dir, "injuries_%d.csv" % season), INJURY_COLUMNS, season,
                  offline, timeout, _downloader)


def fetch_depth_charts(season: int, cache_dir: str, offline: bool = False, timeout: int = 60,
                       _downloader=None) -> list[dict] | None:
    """Rows of nflverse depth_charts_{season}.csv (DEPTH_COLUMNS only), or None when unavailable."""
    return _fetch("depth_charts_%d" % season, DEPTH_CHARTS_URL.format(season=season),
                  os.path.join(cache_dir, "depth_charts_%d.csv" % season), DEPTH_COLUMNS, None,
                  offline, timeout, _downloader)


# ----------------------------------------------------------------------------
# Starters
# ----------------------------------------------------------------------------


def starters_by_team(depth_rows: list[dict], as_of_iso: str) -> dict:
    """{team: {gsis_id: {name, pos_abb, pos_grp}}} from the latest depth chart at or before as_of.

    Per team and per unit (offense / defense; Special Teams dropped) the rows at the latest
    `dt <= as_of_iso` are used; a starter is pos_rank "1" with a non-blank gsis_id whose slot is
    not a kicking/return slot. `pos_grp` in the result is the model group (pos_group(pos_abb)).
    ISO-8601 UTC strings compare lexicographically, so no parsing is needed."""
    latest: dict = {}  # (team, unit) -> dt
    for r in depth_rows:
        team = schema.canonical_code(_s(r.get("team")))
        dt = _s(r.get("dt"))
        if team is None or dt is None or dt > as_of_iso:
            continue
        unit = unit_of(r.get("pos_grp"))
        if unit == "ST":
            continue
        key = (team, unit)
        if latest.get(key) is None or dt > latest[key]:
            latest[key] = dt
    out: dict = {}
    for r in depth_rows:
        team = schema.canonical_code(_s(r.get("team")))
        dt = _s(r.get("dt"))
        if team is None or dt is None:
            continue
        unit = unit_of(r.get("pos_grp"))
        if unit == "ST" or latest.get((team, unit)) != dt:
            continue
        if _s(r.get("pos_rank")) != "1":
            continue
        gsis = _s(r.get("gsis_id"))
        abb = (_s(r.get("pos_abb")) or "").upper()
        if gsis is None or abb in ST_POSITIONS:
            continue
        team_map = out.setdefault(team, {})
        if gsis not in team_map:  # a player in two slots (SS and NB) is one starter
            team_map[gsis] = {"name": _s(r.get("player_name")) or "Unknown", "pos_abb": abb, "pos_grp": pos_group(abb)}
    return out


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------


def unavailable_report() -> dict:
    return {
        "available": False,
        "report_filed": False,
        "players": [],
        "starters_out": 0,
        "starters_doubtful": 0,
        "starters_questionable": 0,
        "total_listed": 0,
        "qb_listed": None,
    }


def _player_sort_key(p: dict) -> tuple:
    return (0 if p["starter"] else 1, STATUS_RANK[p["status"]], p["name"], p["gsis_id"] or "")


def report_for(injury_rows: list[dict], starters: dict, team: str, season: int, week: int,
               listed_qb_id: str | None) -> dict:
    """The CONTRACT 2.6b report for one team-week from the season's injury rows.

    `starters` is starters_by_team(...) (the whole map; this team's entry is looked up).
    A row is "listed" when its report_status is one of STATUSES; practice-only rows count
    toward report_filed but are not players. The latest row wins when a (team, week, gsis_id)
    repeats."""
    code = schema.canonical_code(team) or team
    team_starters = starters.get(code, {})
    filed = False
    listed: dict = {}
    order: list = []
    for r in injury_rows:
        if _s(r.get("season")) != str(season) or _s(r.get("week")) != str(week):
            continue
        if schema.canonical_code(_s(r.get("team"))) != code:
            continue
        filed = True
        status = _s(r.get("report_status"))
        if status is None:
            continue
        status = status.title()
        if status not in STATUS_RANK:
            continue
        gsis = _s(r.get("gsis_id"))
        name = _s(r.get("full_name")) or "Unknown"
        position = _s(r.get("position"))
        key = gsis or ("name:" + name)
        starter = gsis is not None and gsis in team_starters
        group = team_starters[gsis]["pos_grp"] if starter else pos_group(position)
        player = {
            "gsis_id": gsis,
            "name": name,
            "position": position,
            "pos_group": group,
            "status": status,
            "injury": _s(r.get("report_primary_injury")),
            "starter": starter,
            "covered_by_qb_term": group == "QB",
        }
        if key not in listed:
            order.append(key)
        listed[key] = player
    players = sorted((listed[k] for k in order), key=_player_sort_key)
    counted = [p for p in players if p["starter"] and not p["covered_by_qb_term"]]
    qb_status = None
    if listed_qb_id:
        for p in players:
            if p["gsis_id"] == listed_qb_id:
                qb_status = p["status"]
                break
    return {
        "available": True,
        "report_filed": filed,
        "players": players,
        "starters_out": sum(1 for p in counted if p["status"] == "Out"),
        "starters_doubtful": sum(1 for p in counted if p["status"] == "Doubtful"),
        "starters_questionable": sum(1 for p in counted if p["status"] == "Questionable"),
        "total_listed": len(players),
        "qb_listed": qb_status,
    }


# ----------------------------------------------------------------------------
# Elo term (D18.6)
# ----------------------------------------------------------------------------


def injury_elo(report: dict, params: dict) -> float:
    """-(sum of weights[pos_group] * status_mult[status]) over counted starters; 0 when off.

    `params` is the section-2.3 object (uses params["injury"]). Quarterbacks (covered by the
    QB term) and non-starters add nothing; a group without a weight (ST, QB) adds nothing."""
    inj = (params or {}).get("injury") or {}
    if not inj.get("enabled") or not report or not report.get("available"):
        return 0.0
    weights = inj.get("weights") or {}
    mult = inj.get("status_mult") or {}
    total = 0.0
    for p in report.get("players") or []:
        if not p.get("starter") or p.get("covered_by_qb_term"):
            continue
        total += float(weights.get(p.get("pos_group"), 0.0)) * float(mult.get(p.get("status"), 0.0))
    return -total if total else 0.0


def injuries_block(home_report: dict, away_report: dict, params: dict, as_of_iso: str) -> dict:
    """The official.injuries object (CONTRACT 2.6b) for one game."""
    inj = (params or {}).get("injury") or {}
    elo_home = injury_elo(home_report, params)
    elo_away = injury_elo(away_report, params)
    return {
        "enabled": bool(inj.get("enabled")),
        "as_of": as_of_iso,
        "home": home_report,
        "away": away_report,
        "elo_home": elo_home,
        "elo_away": elo_away,
        "elo": elo_home - elo_away,
    }
