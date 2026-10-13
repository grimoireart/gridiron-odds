"""Tests for the D18.6 injury stage: src/injury_history.py, the elo.replay ``extra`` hook,
evaluate.holdout's model_with_injuries row, and tune's coordinate descent + gate."""
import copy
import json
import os
import shutil
import tempfile
import unittest

from src import data, elo, evaluate, injuries, injury_history, schema, tune

FIELDS = [
    "game_id", "season", "game_type", "week", "gameday", "weekday", "gametime", "away_team", "away_score",
    "home_team", "home_score", "location", "result", "total", "overtime", "away_rest", "home_rest",
    "away_moneyline", "home_moneyline", "spread_line", "total_line", "div_game", "roof", "surface",
    "away_qb_id", "home_qb_id", "away_qb_name", "home_qb_name", "stadium",
]


def game(**kw):
    """A normalized game from a raw CSV-shaped row; blanks everywhere not given."""
    base = {k: "" for k in FIELDS}
    base.update(season="2021", game_type="REG", week="1", gameday="2021-09-12", gametime="13:00", location="Home", home_rest="7", away_rest="7")
    base.update({k: ("" if v is None else str(v)) for k, v in kw.items()})
    if not base["game_id"]:
        base["game_id"] = "%s_%02d_%s_%s" % (base["season"], int(base["week"]), base["away_team"], base["home_team"])
    return data.normalize_row(base)


def params(**over):
    p = copy.deepcopy(tune.DEFAULT_PARAMS)
    p["qb"]["enabled"] = False
    p["score"]["league_average_total"] = 45.0
    p["score"]["league_average_season"] = 2020
    for k, v in over.items():
        p["elo"][k] = v
    return p


def legacy_row(team, week, depth_team, formation, gsis_id, depth_position, position, name, season=2021):
    return {"season": str(season), "club_code": team, "week": str(week), "game_type": "REG", "depth_team": str(depth_team),
            "formation": formation, "gsis_id": gsis_id, "position": position, "depth_position": depth_position, "full_name": name}


def injury_row(team, week, gsis_id, position, name, status, season=2021, injury="Knee"):
    return {"season": str(season), "week": str(week), "team": team, "gsis_id": gsis_id, "position": position,
            "full_name": name, "report_status": status, "report_primary_injury": injury}


# A two-team, one-week synthetic fixture: DAL has 3 starters Out (T, WR, CB), one non-starter
# Out, a Probable starting RB and the starting QB Out; PHI has a clean report.
DAL_CHART = [
    legacy_row("DAL", 1, 1, "Offense", "00-0001", "LT", "T", "Left Tackle"),
    legacy_row("DAL", 1, 1, "Offense", "00-0002", "WR", "WR", "Wide One"),
    legacy_row("DAL", 1, 1, "Offense", "00-0003", "RB", "RB", "Back One"),
    legacy_row("DAL", 1, 1, "Offense", "00-0004", "QB", "QB", "Quarter Back"),
    legacy_row("DAL", 1, 2, "Offense", "00-0005", "WR", "WR", "Wide Two"),
    legacy_row("DAL", 1, 1, "Defense", "00-0006", "RCB", "CB", "Corner One"),
    legacy_row("DAL", 1, 1, "Defense", "00-0007", "WILL", "LB", "Will Backer"),    # alias -> WLB -> LB
    legacy_row("DAL", 1, 1, "Defense", "00-0008", "\n    ", "DE", "Slot Blank"),   # blank slot -> roster DE
    legacy_row("DAL", 1, 1, "Special Teams", "00-0009", "K", "K", "The Kicker"),
    legacy_row("DAL", 2, 1, "Offense", "00-0010", "LT", "T", "Next Week Tackle"),   # week 2 chart must not leak
]
PHI_CHART = [
    legacy_row("PHI", 1, 1, "Offense", "00-0101", "LT", "T", "Phi Tackle"),
    legacy_row("PHI", 1, 1, "Defense", "00-0102", "FS", "S", "Phi Safety"),
]
DAL_REPORT = [
    injury_row("DAL", 1, "00-0001", "T", "Left Tackle", "Out"),
    injury_row("DAL", 1, "00-0002", "WR", "Wide One", "Out"),
    injury_row("DAL", 1, "00-0006", "CB", "Corner One", "Out"),
    injury_row("DAL", 1, "00-0005", "WR", "Wide Two", "Out"),           # depth, not a starter
    injury_row("DAL", 1, "00-0003", "RB", "Back One", "Probable"),
    injury_row("DAL", 1, "00-0004", "QB", "Quarter Back", "Out"),       # covered by the QB term
    injury_row("DAL", 1, "00-0010", "T", "Next Week Tackle", "Out"),    # not on the week-1 chart
    injury_row("PHI", 1, "00-0101", "T", "Phi Tackle", ""),             # practice-only row: report filed, nobody listed
]
WEIGHTS = {"OL": 10, "WR_TE": 15, "RB": 5, "DL_EDGE": 20, "LB": 20, "DB": 20}


def fixture_games():
    return [
        game(home_team="DAL", away_team="PHI", week=1, gameday="2021-09-12", home_score=17, away_score=24),
        game(home_team="PHI", away_team="DAL", week=2, gameday="2021-09-19", home_score=20, away_score=23),
    ]


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.games = fixture_games()
        self.depth_rows, stats = injury_history.legacy_to_dt_rows(DAL_CHART + PHI_CHART, self.games)
        self.stats = stats
        self.feat = injury_history.season_features(self.games, 2021, DAL_REPORT, self.depth_rows)

    def test_three_starters_out_gives_the_expected_negative_term(self):
        home = self.feat["games"]["2021_01_PHI_DAL"]["home"]
        self.assertTrue(home["report_filed"])
        self.assertEqual(home["counts"]["OL"]["Out"], 1)
        self.assertEqual(home["counts"]["WR_TE"]["Out"], 1)
        self.assertEqual(home["counts"]["DB"]["Out"], 1)
        self.assertEqual(home["counts"]["RB"]["Probable"], 1)
        self.assertEqual(sum(home["counts"][g]["Out"] for g in schema.INJURY_WEIGHT_GROUPS), 3)  # depth WR + QB excluded
        term = injury_history.side_term(home, WEIGHTS)
        self.assertAlmostEqual(term, -(10 + 15 + 20 + 5 * 0.1))
        no_probable = injury_history.side_term(home, WEIGHTS, {"Out": 1, "Doubtful": 0.75, "Questionable": 0.25, "Probable": 0})
        self.assertAlmostEqual(no_probable, -45.0)
        self.assertEqual(injury_history.side_term(injury_history.empty_side(), WEIGHTS), 0.0)

    def test_term_matches_injuries_injury_elo_on_the_same_report(self):
        as_of = injury_history.as_of_for_gameday("2021-09-12")
        starters = injuries.starters_by_team(self.depth_rows, as_of)
        report = injuries.report_for(DAL_REPORT, starters, "DAL", 2021, 1, "00-0004")
        p = params()
        p["injury"] = {"enabled": True, "weights": WEIGHTS, "status_mult": dict(injury_history.STATUS_MULT), "gate": {"holdout_brier_with": 0.1, "holdout_brier_without": 0.2, "improvement_required": 0.0005}}
        expected = injuries.injury_elo(report, p)
        self.assertLess(expected, 0)
        self.assertAlmostEqual(injury_history.side_term(self.feat["games"]["2021_01_PHI_DAL"]["home"], WEIGHTS), expected)
        self.assertEqual(report["starters_out"], 3)
        self.assertEqual(report["qb_listed"], "Out")

    def test_clean_report_and_extra_map(self):
        away = self.feat["games"]["2021_01_PHI_DAL"]["away"]
        self.assertTrue(away["report_filed"])           # a practice-only row still files the report
        self.assertEqual(injury_history.side_term(away, WEIGHTS), 0.0)
        extra = injury_history.extra_from_features(self.feat["games"], WEIGHTS)
        self.assertEqual(set(extra), {"2021_01_PHI_DAL", "2021_02_DAL_PHI"})
        self.assertLess(extra["2021_01_PHI_DAL"][0], 0)
        self.assertEqual(extra["2021_01_PHI_DAL"][1], 0.0)
        self.assertEqual(extra["2021_02_DAL_PHI"], (0.0, 0.0))  # no week-2 report rows

    def test_legacy_chart_mapping_and_week_isolation(self):
        # dt is the day before the team's game; the week-2 chart never serves a week-1 game.
        week1 = [r for r in self.depth_rows if r["gsis_id"] == "00-0001"][0]
        self.assertEqual(week1["dt"], "2021-09-11T00:00:00Z")
        self.assertEqual(week1["pos_grp"], "Offense")
        self.assertEqual(week1["pos_rank"], "1")
        starters = injuries.starters_by_team(self.depth_rows, injury_history.as_of_for_gameday("2021-09-12"))["DAL"]
        self.assertIn("00-0001", starters)
        self.assertNotIn("00-0010", starters)
        self.assertNotIn("00-0005", starters)   # rank 2
        self.assertNotIn("00-0009", starters)   # kicker
        self.assertEqual(starters["00-0007"]["pos_grp"], "LB")      # WILL -> WLB
        self.assertEqual(starters["00-0008"]["pos_grp"], "DL_EDGE")  # blank slot -> roster DE
        self.assertEqual(self.stats["unmapped_starter_slots"], {})
        self.assertEqual(self.stats["dropped_no_game"], 0)
        self.assertEqual(self.feat["coverage"]["games"], 2)
        self.assertEqual(self.feat["coverage"]["starters_out"], 3)

    def test_legacy_pos_abb_aliases(self):
        self.assertEqual(injury_history.legacy_pos_abb("RE", "DE"), "DE")
        self.assertEqual(injury_history.legacy_pos_abb("MIKE", "LB"), "MLB")
        self.assertEqual(injury_history.legacy_pos_abb("NICKE", "CB"), "NB")
        self.assertEqual(injury_history.legacy_pos_abb("TED", "LB"), "LB")
        self.assertEqual(injury_history.legacy_pos_abb("OC", "C"), "C")
        self.assertEqual(injury_history.legacy_pos_abb("K", "K"), "K")              # kicking slots stay excluded
        self.assertEqual(injury_history.legacy_pos_abb("", "WR"), "WR")
        self.assertEqual(injury_history.legacy_pos_abb("XYZ", "TE"), "TE")
        self.assertEqual(injuries.pos_group(injury_history.legacy_pos_abb("XYZ", "")), "ST")

    def test_as_of_is_the_day_before_gameday(self):
        self.assertEqual(injury_history.as_of_for_gameday("2021-09-12"), "2021-09-11T23:59:59Z")
        self.assertEqual(injury_history.as_of_for_gameday("2022-01-01"), "2021-12-31T23:59:59Z")
        self.assertLess(injury_history.legacy_dt_for_gameday("2021-09-12"), injury_history.as_of_for_gameday("2021-09-12"))


class ReplayHookTests(unittest.TestCase):
    def setUp(self):
        self.games = [
            game(home_team="DAL", away_team="PHI", week=1, gameday="2021-09-12", home_score=17, away_score=24),
            game(home_team="NYG", away_team="WAS", week=1, gameday="2021-09-12", home_score=30, away_score=10),
            game(home_team="PHI", away_team="NYG", week=2, gameday="2021-09-19", home_score=21, away_score=20),
            game(home_team="WAS", away_team="DAL", week=2, gameday="2021-09-19", home_score=14, away_score=35),
            game(home_team="DAL", away_team="NYG", week=3, gameday="2021-09-26"),  # unplayed
        ]
        self.params = params()

    def test_absent_hook_is_byte_identical(self):
        plain = elo.replay(self.games, self.params)
        hooked = elo.replay(self.games, self.params, extra=None)
        self.assertEqual(json.dumps(plain, sort_keys=True), json.dumps(hooked, sort_keys=True))
        self.assertNotIn("extra_home", plain["pregame"]["2021_01_PHI_DAL"])
        empty = elo.replay(self.games, self.params, extra={})
        self.assertEqual(plain["ratings"], empty["ratings"])
        self.assertEqual(empty["pregame"]["2021_01_PHI_DAL"]["extra_home"], 0.0)

    def test_hook_moves_d_and_the_update(self):
        plain = elo.replay(self.games, self.params)
        hooked = elo.replay(self.games, self.params, extra={"2021_01_PHI_DAL": (-45.0, 0.0), "2021_03_NYG_DAL": (0.0, -30.0)})
        pre_p, pre_h = plain["pregame"]["2021_01_PHI_DAL"], hooked["pregame"]["2021_01_PHI_DAL"]
        self.assertAlmostEqual(pre_h["d"], pre_p["d"] - 45.0)
        self.assertEqual(pre_h["elo_home"], pre_p["elo_home"])  # the term is not baked into the rating
        self.assertLess(pre_h["p_home"], pre_p["p_home"])
        self.assertEqual((pre_h["extra_home"], pre_h["extra_away"]), (-45.0, 0.0))
        # DAL lost as a weaker (injured) side: the hooked replay penalises it less.
        self.assertGreater(hooked["ratings"]["DAL"], plain["ratings"]["DAL"])
        self.assertLess(hooked["ratings"]["PHI"], plain["ratings"]["PHI"])
        # other games get 0 and the unplayed game honours the hook too
        self.assertEqual(hooked["pregame"]["2021_01_WAS_NYG"]["extra_home"], 0.0)
        self.assertAlmostEqual(hooked["pregame"]["2021_03_NYG_DAL"]["d"] - hooked["pregame"]["2021_03_NYG_DAL"]["elo_home"]
                               + hooked["pregame"]["2021_03_NYG_DAL"]["elo_away"] - hooked["pregame"]["2021_03_NYG_DAL"]["hfa_elo"], 30.0)

    def test_holdout_row_is_opt_in(self):
        ev = evaluate.holdout(self.games, self.params, 2021)
        self.assertEqual(set(ev), {"season", "samples", "model", "model_no_qb", "market", "blend"})
        extra = {"2021_01_PHI_DAL": (-45.0, 0.0)}
        for mode in ("pregame", "replay"):
            ev2 = evaluate.holdout(self.games, self.params, 2021, injury_extra=extra, injury_mode=mode)
            self.assertIn("model_with_injuries", ev2)
            self.assertEqual(ev2["model"], ev["model"])
            self.assertNotEqual(ev2["model_with_injuries"]["brier"], ev2["model"]["brier"])
            self.assertIn("model_with_injuries", evaluate.format_table(ev2))
        self.assertNotIn("model_with_injuries", evaluate.format_table(ev))
        with self.assertRaises(ValueError):
            evaluate.holdout(self.games, self.params, 2021, injury_extra=extra, injury_mode="bogus")

    def test_pregame_mode_recomputes_p_on_the_plain_d(self):
        rep = elo.replay(self.games, self.params)
        rows = evaluate.holdout_rows_with_extra(self.games, rep, 2021, {"2021_01_PHI_DAL": (-45.0, 10.0)})
        by_id = {r["game_id"]: r for r in rows}
        self.assertAlmostEqual(by_id["2021_01_PHI_DAL"]["p_model"], elo.win_prob(rep["pregame"]["2021_01_PHI_DAL"]["d"] - 55.0))
        self.assertEqual(by_id["2021_01_WAS_NYG"]["p_model"], rep["pregame"]["2021_01_WAS_NYG"]["p_home"])
        # the plain replay is untouched and the first-week games (equal ratings) differ only by the term
        self.assertEqual(rep["pregame"]["2021_01_PHI_DAL"]["p_home"], rep["pregame"]["2021_01_WAS_NYG"]["p_home"])


class GateAndDescentTests(unittest.TestCase):
    def test_gate_enabled_only_at_or_beyond_the_threshold(self):
        self.assertTrue(tune.injury_gate_enabled(0.2200, 0.2205, 0.0005))
        self.assertTrue(tune.injury_gate_enabled(0.2190, 0.2205, 0.0005))
        self.assertFalse(tune.injury_gate_enabled(0.2201, 0.2205, 0.0005))
        self.assertFalse(tune.injury_gate_enabled(0.2205, 0.2205, 0.0005))
        self.assertFalse(tune.injury_gate_enabled(0.2210, 0.2205, 0.0005))
        self.assertFalse(tune.injury_gate_enabled(None, 0.2205, 0.0005))
        self.assertFalse(tune.injury_gate_enabled(0.2200, None, 0.0005))
        self.assertTrue(tune.injury_gate_enabled(0.2205, 0.2205, 0.0))

    def test_default_block_matches_the_schema_shape(self):
        inj = tune.DEFAULT_PARAMS["injury"]
        self.assertEqual(set(inj), schema.PARAMS_INJURY_KEYS)
        self.assertEqual(set(inj["weights"]), set(schema.INJURY_WEIGHT_GROUPS))
        self.assertEqual(set(inj["status_mult"]), set(schema.INJURY_STATUSES))
        self.assertEqual(set(inj["gate"]), schema.PARAMS_INJURY_GATE_KEYS)
        self.assertFalse(inj["enabled"])

    def _synthetic_season(self):
        """Eight teams, six weeks: the away side always has 3 OL starters Out and loses."""
        teams = ["DAL", "PHI", "NYG", "WAS", "CHI", "DET", "GB", "MIN"]
        games, features = [], {}
        day = 12
        for week in range(1, 7):
            rot = teams[week:] + teams[:week]
            for i in range(0, 8, 2):
                home, away = rot[i], rot[i + 1]
                g = game(home_team=home, away_team=away, week=week, gameday="2021-09-%02d" % day, home_score=27, away_score=13)
                games.append(g)
                side = injury_history.empty_side()
                side["report_filed"] = True
                side["counts"]["OL"]["Out"] = 3
                features[g["game_id"]] = {"home": injury_history.empty_side(), "away": side}
            day += 1
        return games, features

    def test_coordinate_descent_prefers_a_positive_weight_when_it_helps(self):
        games, features = self._synthetic_season()
        p = params(hfa=0, rest_per_day=0)
        for mode in tune.INJURY_MODES:
            fit = tune.coordinate_descent_injury(games, p, features, seasons=(2021, 2021), log=None, mode=mode)
            self.assertEqual(fit["mode"], mode)
            self.assertEqual(set(fit["weights"]), set(schema.INJURY_WEIGHT_GROUPS))
            for g, w in fit["weights"].items():
                self.assertIn(w, tune.INJURY_GRID)
            self.assertEqual(fit["weights"]["OL"], max(tune.INJURY_GRID))
            for g in ("WR_TE", "RB", "DL_EDGE", "LB", "DB"):
                self.assertEqual(fit["weights"][g], 0)  # groups with no signal stay at 0
            self.assertLess(fit["brier"], fit["brier_without"])
            self.assertGreaterEqual(fit["sweeps"], 2)  # the confirming sweep
            self.assertEqual(fit["n"], len(games))
            self.assertTrue(fit["trace"])
        with self.assertRaises(ValueError):
            tune.injury_objective(games, p, features, {"OL": 5}, seasons=(2021, 2021), mode="bogus")

    def test_pregame_objective_with_zero_weights_is_the_plain_brier(self):
        games, features = self._synthetic_season()
        p = params(hfa=0, rest_per_day=0)
        rep = elo.replay(games, p)
        plain = evaluate.season_brier(games, rep, (2021, 2021))
        zero = {g: 0 for g in schema.INJURY_WEIGHT_GROUPS}
        self.assertEqual(tune.injury_objective(games, p, features, zero, seasons=(2021, 2021), mode="pregame", replay_result=rep), plain)
        self.assertEqual(tune.injury_objective(games, p, features, zero, seasons=(2021, 2021), mode="replay"), plain)

    def test_tune_injury_without_history_stays_off(self):
        games, _ = self._synthetic_season()
        block, tuning, ev = tune.tune_injury(games, params(), None)
        self.assertFalse(block["enabled"])
        self.assertEqual(block["gate"]["holdout_brier_with"], None)
        self.assertFalse(tuning["available"])
        self.assertIsNone(ev)
        self.assertIn("history unavailable", tune.format_injury_table({"injury": block, "injury_tuning": tuning}))

    def test_tune_injury_end_to_end_on_the_synthetic_season(self):
        games, features = self._synthetic_season()
        p = params(hfa=0, rest_per_day=0)
        # the synthetic data lives in 2021: tune and gate on that season explicitly
        fit = tune.coordinate_descent_injury(games, p, features, seasons=(2021, 2021))
        gate, ev = tune.injury_gate(games, p, features, fit["weights"], season=2021)
        self.assertEqual(gate["mode"], tune.INJURY_MODE)
        self.assertEqual(set(gate) - {"enabled", "season", "n", "mode"}, schema.PARAMS_INJURY_GATE_KEYS)
        self.assertEqual(gate["holdout_brier_without"], ev["model"]["brier"])
        self.assertEqual(gate["holdout_brier_with"], ev["model_with_injuries"]["brier"])
        self.assertEqual(gate["enabled"], tune.injury_gate_enabled(gate["holdout_brier_with"], gate["holdout_brier_without"], gate["improvement_required"]))
        self.assertTrue(gate["enabled"])
        self.assertEqual(gate["n"], len(games))
        # the full stage writes an exact section-2.3 block that the schema accepts, plus the diagnostics
        history = {"features": features, "coverage": {"2021": {"skipped": False, "depth_format": "legacy", "games": len(games),
                                                                "games_both_filed": len(games), "starters_per_side_sum": 44 * len(games),
                                                                "starters_out": 3 * len(games), "starters_doubtful": 0, "starters_questionable": 0,
                                                                "starters_probable": 0}}, "notes": []}
        block, tuning, ev2 = tune.tune_injury(games, p, history, seasons=(2021, 2021), holdout_season=2021)
        self.assertEqual(set(block), schema.PARAMS_INJURY_KEYS)
        problems = []
        schema._check_params_injury(block, problems)
        self.assertEqual(problems, [])
        self.assertEqual(block["weights"], fit["weights"])
        self.assertEqual(block["enabled"], gate["enabled"])
        self.assertEqual(tuning["mode"], tune.INJURY_MODE)
        self.assertEqual(tuning["alternative"]["mode"], [m for m in tune.INJURY_MODES if m != tune.INJURY_MODE][0])
        self.assertIn("model_with_injuries", ev2)
        self.assertEqual(tuning["coverage"]["2021"]["starters_per_side"], 22.0)
        table = tune.format_injury_table({"injury": block, "injury_tuning": tuning})
        self.assertIn("enabled=True", table)
        self.assertIn("for the record", table)


LEGACY_CSV = ("season,club_code,week,game_type,depth_team,last_name,first_name,football_name,formation,gsis_id,jersey_number,position,elias_id,depth_position,full_name\n"
              "2021,DAL,1,REG,1,Tackle,Left,Left,Offense,00-0001,77,T,X,LT,Left Tackle\n"
              "2021,DAL,1,REG,1,One,Wide,Wide,Offense,00-0002,88,WR,X,WR,Wide One\n")
INJ_CSV = ("season,game_type,team,week,gsis_id,position,full_name,first_name,last_name,report_primary_injury,report_secondary_injury,report_status,practice_primary_injury,practice_secondary_injury,practice_status,date_modified\n"
           "2021,REG,DAL,1,00-0001,T,Left Tackle,Left,Tackle,Knee,,Out,Knee,,Did Not Participate In Practice,2021-09-10T12:00:00Z\n")


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridiron-injhist-")
        self.games = fixture_games()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _downloader(self, missing=()):
        def dl(url, dest, timeout):
            name = url.rsplit("/", 1)[-1]
            if name in missing:
                raise OSError("HTTP 404 %s" % name)
            text = LEGACY_CSV if name.startswith("depth_charts") else INJ_CSV
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(text)
        return dl

    def test_downloads_once_then_reuses_the_cache(self):
        calls = []
        base = self._downloader()

        def counting(url, dest, timeout):
            calls.append(url)
            base(url, dest, timeout)
        hist = injury_history.build_history(self.games, seasons=(2021,), cache_dir=self.dir, _downloader=counting)
        self.assertEqual(len(calls), 2)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "depth_charts_2021.csv")))
        self.assertEqual(injury_history.depth_chart_format(os.path.join(self.dir, "depth_charts_2021.csv")), "legacy")
        self.assertEqual(hist["notes"], [])
        self.assertEqual(hist["coverage"][2021]["depth_format"], "legacy")
        home = hist["features"]["2021_01_PHI_DAL"]["home"]
        self.assertEqual(home["counts"]["OL"]["Out"], 1)
        self.assertEqual(home["starters"], 2)
        # second run: nothing downloaded, same features
        again = injury_history.build_history(self.games, seasons=(2021,), cache_dir=self.dir, _downloader=counting)
        self.assertEqual(len(calls), 2)
        self.assertEqual(again["features"], hist["features"])
        self.assertFalse([f for f in os.listdir(self.dir) if f.endswith(".tmp")])

    def test_missing_year_is_skipped_with_a_note(self):
        hist = injury_history.build_history(self.games, seasons=(2021,), cache_dir=self.dir, _downloader=self._downloader(missing=("depth_charts_2021.csv",)))
        self.assertEqual(hist["features"], {})
        self.assertTrue(hist["coverage"][2021]["skipped"])
        self.assertEqual(len(hist["notes"]), 1)
        self.assertIn("depth_charts_2021.csv unavailable", hist["notes"][0])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "depth_charts_2021.csv")))

    def test_offline_without_cache_skips(self):
        hist = injury_history.build_history(self.games, seasons=(2021,), cache_dir=self.dir, offline=True, _downloader=self._downloader())
        self.assertEqual(hist["features"], {})
        self.assertTrue(hist["coverage"][2021]["skipped"])

    def test_html_download_is_not_cached(self):
        def html(url, dest, timeout):
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write("<html>rate limited</html>")
        self.assertIsNone(injury_history.fetch_depth_chart_file(2021, self.dir, _downloader=html))
        self.assertEqual([f for f in os.listdir(self.dir)], [])


if __name__ == "__main__":
    unittest.main()
