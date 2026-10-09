"""nflverse games.csv: safe fetch/cache (D12) and normalized loading (D1, D7).

Public surface (see docs/CONTRACT.md):
    load_games(path) -> list[dict]            normalized, chronologically sorted games
    fetch_games(url, cache_path, offline=False, timeout=60) -> dict   fetch report
    validate_csv(path, min_rows=0) -> list[str]   problems (empty = valid)
    sha256_file(path) -> str
    kickoff_utc(gameday, gametime) -> (iso_utc, time_known)
    sort_key(game) -> tuple
    normalize_row(row) -> dict

Stdlib only. Loud failure: DataError is raised; the cache is never replaced
with an invalid download.
"""
from __future__ import annotations

import csv
import hashlib
import os
import shutil
import tempfile
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import schema

REQUIRED_COLUMNS = (
    "game_id",
    "season",
    "game_type",
    "week",
    "gameday",
    "gametime",
    "away_team",
    "away_score",
    "home_team",
    "home_score",
    "location",
    "away_rest",
    "home_rest",
    "away_moneyline",
    "home_moneyline",
    "spread_line",
    "total_line",
    "away_qb_id",
    "home_qb_id",
    "away_qb_name",
    "home_qb_name",
)
OPTIONAL_COLUMNS = ("weekday", "result", "total", "overtime", "div_game", "roof", "surface", "stadium")

# A full NFL season has 256 (31 teams, 1999-2001: 248) to 272 REG games plus 11-13 playoff games.
SEASON_ROWS_MIN = 240
SEASON_ROWS_MAX = 300
ROW_COUNT_SLACK = 1000  # D12: downloaded rows must be >= cached rows - 1000

_LEAGUE_TZ = ZoneInfo(schema.LEAGUE_TZ)


class DataError(Exception):
    """Raised when the games file cannot be fetched or does not validate."""


# ----------------------------------------------------------------------------
# Field parsing (never '' — always a typed value or None)
# ----------------------------------------------------------------------------


def _str(v) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


def _int(v) -> int | None:
    s = _str(v)
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        f = float(s)
        if f != int(f):
            raise
        return int(f)


def _float(v) -> float | None:
    s = _str(v)
    return None if s is None else float(s)


def _bool01(v) -> bool | None:
    i = _int(v)
    return None if i is None else bool(i)


def kickoff_utc(gameday: str, gametime: str | None) -> tuple[str, bool]:
    """D7: interpret gameday + gametime as America/New_York; blank time => 13:00, time_known=false.

    Returns (ISO-8601 UTC string 'YYYY-MM-DDTHH:MM:SSZ', time_known).
    """
    t = _str(gametime)
    known = t is not None
    hh, mm = (t or schema.DEFAULT_KICKOFF_ET).split(":")[:2]
    y, m, d = (int(x) for x in gameday.split("-"))
    local = datetime(y, m, d, int(hh), int(mm), tzinfo=_LEAGUE_TZ)
    utc = local.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ"), known


def sort_key(game: dict) -> tuple:
    """D1 chronology key: (season, gameday, gametime-missing-last, gametime, game_id)."""
    t = game.get("gametime")
    return (game["season"], game["gameday"], 1 if t is None else 0, t or "", game["game_id"])


def normalize_row(row: dict) -> dict:
    """Turn one raw CSV row into the normalized game dict defined in CONTRACT.md."""
    home_raw = _str(row["home_team"])
    away_raw = _str(row["away_team"])
    home = schema.canonical_code(home_raw)
    away = schema.canonical_code(away_raw)
    if home is None or away is None:
        raise DataError("unknown team code in %s: home=%r away=%r" % (row.get("game_id"), home_raw, away_raw))
    gameday = _str(row["gameday"])
    gametime = _str(row["gametime"])
    kick, known = kickoff_utc(gameday, gametime)
    home_score = _int(row["home_score"])
    away_score = _int(row["away_score"])
    played = home_score is not None and away_score is not None
    g = {
        "game_id": _str(row["game_id"]),
        "season": _int(row["season"]),
        "game_type": _str(row["game_type"]),
        "week": _int(row["week"]),
        "gameday": gameday,
        "gametime": gametime,
        "weekday": _str(row.get("weekday")),
        "league_date": gameday,
        "kickoff_utc": kick,
        "time_known": known,
        "home": home,
        "away": away,
        "home_raw": home_raw,
        "away_raw": away_raw,
        "location": _str(row["location"]),
        "neutral": (_str(row["location"]) or "").lower() == "neutral",
        "home_score": home_score,
        "away_score": away_score,
        "played": played,
        "result": (home_score - away_score) if played else None,
        "total": (home_score + away_score) if played else None,
        "overtime": _bool01(row.get("overtime")),
        "home_rest": _int(row["home_rest"]),
        "away_rest": _int(row["away_rest"]),
        "home_moneyline": _int(row["home_moneyline"]),
        "away_moneyline": _int(row["away_moneyline"]),
        "spread_line": _float(row["spread_line"]),
        "total_line": _float(row["total_line"]),
        "home_qb_id": _str(row["home_qb_id"]),
        "away_qb_id": _str(row["away_qb_id"]),
        "home_qb_name": _str(row["home_qb_name"]),
        "away_qb_name": _str(row["away_qb_name"]),
        "div_game": _bool01(row.get("div_game")),
        "roof": _str(row.get("roof")),
        "surface": _str(row.get("surface")),
        "stadium": _str(row.get("stadium")),
    }
    g["sort_key"] = sort_key(g)
    return g


def load_games(path: str) -> list[dict]:
    """Load and normalize games.csv; rows come back sorted by the D1 chronology key."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise DataError("games.csv is missing required columns: %s" % missing)
        games = [normalize_row(r) for r in reader]
    games.sort(key=lambda g: g["sort_key"])
    return games


# ----------------------------------------------------------------------------
# Validation and fetch (D12)
# ----------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_rows(path: str) -> int:
    with open(path, newline="", encoding="utf-8") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def validate_csv(path: str, min_rows: int = 0) -> list[str]:
    """D12 checks on a candidate games.csv. Returns a list of problems (empty = valid)."""
    problems: list[str] = []
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError as exc:
        return ["cannot read %s: %s" % (path, exc)]
    if not head:
        return ["file is empty"]
    if head.lstrip()[:1] in (b"<", b"{"):
        return ["file does not look like CSV (starts with %r)" % head.lstrip()[:15].decode("latin-1")]
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            cols = reader.fieldnames or []
            missing = [c for c in REQUIRED_COLUMNS if c not in cols]
            if missing:
                return ["missing required columns: %s" % missing]
            ids = set()
            seasons: dict[int, int] = {}
            n = 0
            for r in reader:
                n += 1
                gid = _str(r["game_id"])
                if not gid:
                    problems.append("row %d: blank game_id" % n)
                elif gid in ids:
                    problems.append("duplicate game_id %s" % gid)
                ids.add(gid)
                try:
                    season = _int(r["season"])
                    _int(r["week"])
                    hs, as_ = _int(r["home_score"]), _int(r["away_score"])
                    for k in ("home_rest", "away_rest", "home_moneyline", "away_moneyline"):
                        _int(r[k])
                    for k in ("spread_line", "total_line"):
                        _float(r[k])
                except ValueError as exc:
                    problems.append("%s: unparseable number (%s)" % (gid, exc))
                    continue
                if season is None:
                    problems.append("%s: blank season" % gid)
                    continue
                seasons[season] = seasons.get(season, 0) + 1
                if (hs is None) != (as_ is None):
                    problems.append("%s: scores must be both present or both blank" % gid)
                if (hs is not None and hs < 0) or (as_ is not None and as_ < 0):
                    problems.append("%s: negative score" % gid)
                if _str(r["game_type"]) not in schema.GAME_TYPES:
                    problems.append("%s: game_type %r not allowed" % (gid, r["game_type"]))
                for k in ("home_team", "away_team"):
                    if schema.canonical_code(_str(r[k])) is None:
                        problems.append("%s: unknown team %r" % (gid, r[k]))
                if not _str(r["gameday"]):
                    problems.append("%s: blank gameday" % gid)
                if len(problems) > 50:
                    problems.append("... stopping after 50 problems")
                    return problems
    except (UnicodeDecodeError, csv.Error) as exc:
        return ["cannot parse CSV: %s" % exc]
    if n < min_rows:
        problems.append("row count %d is below the minimum %d" % (n, min_rows))
    if not seasons:
        problems.append("no rows")
    for season, cnt in sorted(seasons.items()):
        if season == max(seasons):
            continue  # the current season fills in as the schedule is published
        if not SEASON_ROWS_MIN <= cnt <= SEASON_ROWS_MAX:
            problems.append("season %d has %d rows (expected %d-%d)" % (season, cnt, SEASON_ROWS_MIN, SEASON_ROWS_MAX))
    if max(seasons, default=0) and not 200 <= seasons[max(seasons)] <= SEASON_ROWS_MAX:
        problems.append("current season %d has %d rows (expected 200-%d)" % (max(seasons), seasons[max(seasons)], SEASON_ROWS_MAX))
    if min(seasons, default=1999) != 1999:
        problems.append("first season is %d, expected 1999" % min(seasons))
    return problems


def _download(url: str, dest: str, timeout: int) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "gridiron-odds/1.0 (stdlib urllib)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def fetch_games(url: str, cache_path: str, offline: bool = False, timeout: int = 60, _downloader=None) -> dict:
    """Refresh data/games.csv safely (D12).

    Downloads to a temp file next to the cache, validates it, then atomically
    replaces the cache. On any failure the old cache is kept untouched and
    DataError is raised (loud). With offline=True the cache is used as-is.

    Returns {"path", "sha256", "rows", "fetched": bool, "source": "download"|"cache", "url"}.
    `_downloader(url, dest, timeout)` is an injection point for tests.
    """
    have_cache = os.path.exists(cache_path)
    if offline:
        if not have_cache:
            raise DataError("GRIDIRON_OFFLINE is set but no cache exists at %s" % cache_path)
        problems = validate_csv(cache_path)
        if problems:
            raise DataError("cached %s failed validation: %s" % (cache_path, problems[:5]))
        return {"path": cache_path, "sha256": sha256_file(cache_path), "rows": count_rows(cache_path), "fetched": False, "source": "cache", "url": url}

    os.makedirs(os.path.dirname(os.path.abspath(cache_path)) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="games.", suffix=".tmp", dir=os.path.dirname(os.path.abspath(cache_path)))
    os.close(fd)
    try:
        try:
            (_downloader or _download)(url, tmp, timeout)
        except Exception as exc:  # network, HTTP, timeout
            raise DataError("download of %s failed: %s" % (url, exc)) from exc
        min_rows = max(0, count_rows(cache_path) - ROW_COUNT_SLACK) if have_cache else 1
        problems = validate_csv(tmp, min_rows=min_rows)
        if problems:
            raise DataError("downloaded games.csv failed validation (cache kept): %s" % problems[:5])
        os.replace(tmp, cache_path)  # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"path": cache_path, "sha256": sha256_file(cache_path), "rows": count_rows(cache_path), "fetched": True, "source": "download", "url": url}


def max_result_date(games: list[dict]) -> str | None:
    """Latest league_date with a final score (build.data.max_result_date)."""
    dates = [g["league_date"] for g in games if g["played"]]
    return max(dates) if dates else None
