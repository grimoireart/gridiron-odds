"""Tests for src/data.py: aliasing, kickoff conversion, sort key, validation, atomic fetch."""
import csv
import io
import os
import shutil
import tempfile
import unittest

from src import data, schema

FIELDS = [
    "game_id", "season", "game_type", "week", "gameday", "weekday", "gametime", "away_team", "away_score",
    "home_team", "home_score", "location", "result", "total", "overtime", "away_rest", "home_rest",
    "away_moneyline", "home_moneyline", "spread_line", "total_line", "div_game", "roof", "surface",
    "away_qb_id", "home_qb_id", "away_qb_name", "home_qb_name", "stadium",
]


def row(**kw):
    """A complete raw CSV row dict with blanks everywhere not given."""
    base = {k: "" for k in FIELDS}
    base.update(
        game_id="2026_05_PHI_JAX", season="2026", game_type="REG", week="5", gameday="2026-10-11", weekday="Sunday",
        gametime="09:30", away_team="PHI", home_team="JAX", location="Home", away_rest="7", home_rest="7",
        away_moneyline="310", home_moneyline="-395", spread_line="7.5", total_line="41.5",
        away_qb_id="00-0036389", home_qb_id="00-0036971", away_qb_name="Jalen Hurts", home_qb_name="Trevor Lawrence",
    )
    base.update(kw)
    return base


def write_csv(path, rows, fields=FIELDS):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def season_rows(season, n, start_id=0):
    """n plausible played rows for one season (teams cycle through the canonical list)."""
    out = []
    teams = list(schema.TEAMS)
    for i in range(n):
        h, a = teams[i % 32], teams[(i + 7) % 32]
        out.append(row(game_id="%d_%02d_%s_%s" % (season, i // 16 + 1, a, h), season=str(season), week=str(i // 16 + 1),
                       gameday="%d-09-%02d" % (season, 1 + (i % 28)), gametime="13:00", away_team=a, home_team=h,
                       away_score=str(17 + i % 5), home_score=str(20 + i % 7)))
    return out


class NormalizeTests(unittest.TestCase):
    def test_alias_maps_old_codes(self):
        g = data.normalize_row(row(home_team="STL", away_team="OAK"))
        self.assertEqual((g["home"], g["away"]), ("LA", "LV"))
        self.assertEqual((g["home_raw"], g["away_raw"]), ("STL", "OAK"))
        self.assertEqual(data.normalize_row(row(home_team="SD"))["home"], "LAC")
        self.assertEqual(schema.canonical_code("LAR"), "LA")
        self.assertIsNone(schema.canonical_code("XXX"))

    def test_unknown_team_is_loud(self):
        with self.assertRaises(data.DataError):
            data.normalize_row(row(home_team="ZZZ"))

    def test_kickoff_conversion_edt(self):
        iso, known = data.kickoff_utc("2026-10-11", "09:30")
        self.assertEqual(iso, "2026-10-11T13:30:00Z")  # EDT = UTC-4
        self.assertTrue(known)

    def test_kickoff_conversion_est(self):
        iso, known = data.kickoff_utc("2026-12-13", "20:20")
        self.assertEqual(iso, "2026-12-14T01:20:00Z")  # EST = UTC-5, crosses midnight
        self.assertTrue(known)

    def test_blank_time_defaults_to_1pm_et_and_flags(self):
        iso, known = data.kickoff_utc("1999-09-12", "")
        self.assertEqual(iso, "1999-09-12T17:00:00Z")
        self.assertFalse(known)
        g = data.normalize_row(row(gametime=""))
        self.assertIsNone(g["gametime"])
        self.assertFalse(g["time_known"])
        self.assertEqual(g["league_date"], "2026-10-11")

    def test_blank_fields_become_none_not_empty_string(self):
        g = data.normalize_row(row(home_score="", away_score="", home_moneyline="", spread_line="", home_qb_id="", home_qb_name=""))
        for k in ("home_score", "away_score", "home_moneyline", "spread_line", "home_qb_id", "home_qb_name", "result", "total"):
            self.assertIsNone(g[k], k)
        self.assertFalse(g["played"])
        for v in g.values():
            self.assertNotEqual(v, "")

    def test_played_game_fields(self):
        g = data.normalize_row(row(home_score="24", away_score="16", overtime="0"))
        self.assertTrue(g["played"])
        self.assertEqual((g["result"], g["total"], g["overtime"]), (8, 40, False))
        self.assertEqual(g["spread_line"], 7.5)
        self.assertEqual(g["home_moneyline"], -395)
        self.assertFalse(g["neutral"])
        self.assertTrue(data.normalize_row(row(location="Neutral"))["neutral"])

    def test_sort_key_missing_time_last_then_time_then_id(self):
        a = data.normalize_row(row(game_id="2026_05_A_B", gametime="", home_team="KC", away_team="DEN"))
        b = data.normalize_row(row(game_id="2026_05_C_D", gametime="16:25", home_team="KC", away_team="DEN"))
        c = data.normalize_row(row(game_id="2026_05_E_F", gametime="13:00", home_team="KC", away_team="DEN"))
        d = data.normalize_row(row(game_id="2026_05_A_A", gametime="13:00", home_team="KC", away_team="DEN"))
        e = data.normalize_row(row(game_id="2025_01_Z_Z", season="2025", gameday="2026-10-12", gametime="13:00", home_team="KC", away_team="DEN"))
        ordered = sorted([a, b, c, d, e], key=data.sort_key)
        self.assertEqual([g["game_id"] for g in ordered], ["2025_01_Z_Z", "2026_05_A_A", "2026_05_E_F", "2026_05_C_D", "2026_05_A_B"])

    def test_load_games_sorted_and_normalized(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "g.csv")
            rows = [row(game_id="2026_05_CHI_GB", gametime="13:00", home_team="GB", away_team="CHI"),
                    row(game_id="2026_05_PHI_JAX", gametime="09:30")]
            write_csv(p, rows)
            games = data.load_games(p)
            self.assertEqual([g["game_id"] for g in games], ["2026_05_PHI_JAX", "2026_05_CHI_GB"])
            self.assertEqual(games[0]["kickoff_utc"], "2026-10-11T13:30:00Z")
        finally:
            shutil.rmtree(tmp)

    def test_load_games_missing_column_is_loud(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "g.csv")
            fields = [f for f in FIELDS if f != "home_qb_id"]
            write_csv(p, [{k: v for k, v in row().items() if k != "home_qb_id"}], fields)
            with self.assertRaises(data.DataError):
                data.load_games(p)
        finally:
            shutil.rmtree(tmp)


class ValidateAndFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "games.csv")
        self.good_rows = []
        for s in range(1999, 2004):
            self.good_rows += season_rows(s, 260)
        self.good_rows += season_rows(2004, 230)  # current (partial) season
        write_csv(self.cache, self.good_rows)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_good_file_validates(self):
        self.assertEqual(data.validate_csv(self.cache), [])

    def test_rejects_html_page(self):
        p = os.path.join(self.tmp, "bad.csv")
        with open(p, "w") as fh:
            fh.write("<!DOCTYPE html><html><body>Rate limited</body></html>")
        problems = data.validate_csv(p)
        self.assertTrue(problems and "does not look like CSV" in problems[0])

    def test_rejects_duplicate_ids_and_unpaired_scores(self):
        rows = list(self.good_rows)
        rows.append(dict(rows[0]))  # duplicate id
        rows.append(row(game_id="2004_09_KC_DEN", season="2004", home_team="DEN", away_team="KC", home_score="21", away_score=""))
        p = os.path.join(self.tmp, "dup.csv")
        write_csv(p, rows)
        problems = data.validate_csv(p)
        self.assertTrue(any("duplicate game_id" in x for x in problems))
        self.assertTrue(any("both present or both blank" in x for x in problems))

    def test_rejects_unknown_team_and_implausible_season(self):
        rows = list(self.good_rows) + [row(game_id="2004_09_QQ_DEN", season="2004", home_team="DEN", away_team="QQ")]
        rows += season_rows(2001, 100, start_id=500)  # inflates 2001 to 360 rows -> implausible
        for i, r in enumerate(rows[-100:]):
            r["game_id"] = "2001_99_X_%d" % i
        p = os.path.join(self.tmp, "bad.csv")
        write_csv(p, rows)
        problems = data.validate_csv(p)
        self.assertTrue(any("unknown team" in x for x in problems))
        self.assertTrue(any("season 2001 has" in x for x in problems))

    def test_fetch_offline_uses_cache(self):
        rep = data.fetch_games("http://unused", self.cache, offline=True)
        self.assertEqual(rep["source"], "cache")
        self.assertFalse(rep["fetched"])
        self.assertEqual(rep["rows"], len(self.good_rows))
        self.assertEqual(len(rep["sha256"]), 64)

    def test_fetch_offline_without_cache_is_loud(self):
        with self.assertRaises(data.DataError):
            data.fetch_games("http://unused", os.path.join(self.tmp, "nope.csv"), offline=True)

    def test_fetch_replaces_cache_atomically_on_success(self):
        new_rows = list(self.good_rows) + [row(game_id="2004_15_KC_DEN", season="2004", home_team="DEN", away_team="KC")]

        def fake(url, dest, timeout):
            write_csv(dest, new_rows)

        before = data.sha256_file(self.cache)
        rep = data.fetch_games("http://x", self.cache, _downloader=fake)
        self.assertTrue(rep["fetched"])
        self.assertEqual(rep["rows"], len(new_rows))
        self.assertNotEqual(rep["sha256"], before)
        self.assertEqual(rep["sha256"], data.sha256_file(self.cache))
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])

    def test_bad_download_keeps_old_cache(self):
        before = data.sha256_file(self.cache)

        def html(url, dest, timeout):
            with open(dest, "w") as fh:
                fh.write("<html>502 Bad Gateway</html>")

        with self.assertRaises(data.DataError):
            data.fetch_games("http://x", self.cache, _downloader=html)
        self.assertEqual(data.sha256_file(self.cache), before)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])

    def test_row_count_drop_keeps_old_cache(self):
        before = data.sha256_file(self.cache)

        def truncated(url, dest, timeout):
            write_csv(dest, self.good_rows[:200])  # far more than 1000 rows fewer

        with self.assertRaises(data.DataError):
            data.fetch_games("http://x", self.cache, _downloader=truncated)
        self.assertEqual(data.sha256_file(self.cache), before)

    def test_network_failure_keeps_old_cache(self):
        before = data.sha256_file(self.cache)

        def boom(url, dest, timeout):
            raise OSError("connection refused")

        with self.assertRaises(data.DataError):
            data.fetch_games("http://x", self.cache, _downloader=boom)
        self.assertEqual(data.sha256_file(self.cache), before)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])


@unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(__file__), "..", "data", "games.csv")), "no cached games.csv")
class RealCsvTests(unittest.TestCase):
    def test_real_cache_loads_and_validates(self):
        path = os.path.join(os.path.dirname(__file__), "..", "data", "games.csv")
        self.assertEqual(data.validate_csv(path), [])
        games = data.load_games(path)
        self.assertGreater(len(games), 7000)
        self.assertEqual(sorted({g["home"] for g in games} | {g["away"] for g in games}), list(schema.TEAMS))
        keys = [g["sort_key"] for g in games]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(all(g["time_known"] is False for g in games if g["gametime"] is None))


if __name__ == "__main__":
    unittest.main()
