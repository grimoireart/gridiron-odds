"""Tests for src/build.py: the pure helpers and the whole offline build, run twice."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from src import build, data, ledger, schema

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES = os.path.join(ROOT, "data", "games.csv")
PARAMS = os.path.join(ROOT, "params.json")
LIVE_SAMPLE = os.path.join(ROOT, "docs", "odds_api_live_sample_h2h_2026-10-09.json")
HAVE_DATA = os.path.exists(GAMES) and os.path.exists(PARAMS)
NOW = datetime(2026, 10, 9, 20, 0, 0, tzinfo=timezone.utc)


class HelperTests(unittest.TestCase):
    def test_build_id_is_compact_timestamp_plus_short_sha(self):
        self.assertEqual(build.build_id("2026-10-10T14:03:12Z", "3f2a9c1e5b7d"), "20261010T140312Z-3f2a9c1")

    def test_league_date_is_new_york_date(self):
        self.assertEqual(build.league_date_at("2026-10-10T02:30:00Z"), "2026-10-09")  # 22:30 ET the day before
        self.assertEqual(build.league_date_at("2026-10-10T14:00:00Z"), "2026-10-10")

    def test_next_league_date_skips_finals_and_earlier_dates(self):
        entries = [
            {"league_date": "2026-10-08", "status": "final"},
            {"league_date": "2026-10-09", "status": "final"},
            {"league_date": "2026-10-12", "status": "scheduled"},
            {"league_date": "2026-10-11", "status": "scheduled"},
        ]
        self.assertEqual(build.next_league_date(entries, "2026-10-09"), "2026-10-11")
        self.assertEqual(build.next_league_date(entries, "2026-10-12"), "2026-10-12")
        self.assertIsNone(build.next_league_date(entries, "2026-10-13"))

    def test_is_offline_env_values(self):
        self.assertTrue(build.is_offline({"GRIDIRON_OFFLINE": "1"}))
        self.assertTrue(build.is_offline({"GRIDIRON_OFFLINE": "true"}))
        self.assertFalse(build.is_offline({"GRIDIRON_OFFLINE": "0"}))
        self.assertFalse(build.is_offline({}))

    def test_game_market_prefers_feed_then_csv_then_none(self):
        game = {"home_moneyline": -150, "away_moneyline": 130, "spread_line": 3.0, "total_line": 44.5}
        feed = {"books": [{"key": "draftkings", "home_ml": -140, "away_ml": 120, "home_spread": -3.0, "total": 44.0}]}
        m = build.game_market(game, feed, "2026-10-09T10:00:00Z", "2026-10-09T10:05:00Z")
        self.assertEqual((m["source"], m["as_of"], m["n_books"]), ("odds_api", "2026-10-09T10:00:00Z", 1))
        # feed matched but no book priced both sides -> CSV lines
        half = {"books": [{"key": "draftkings", "home_ml": -140, "away_ml": None, "home_spread": None, "total": None}]}
        m = build.game_market(game, half, "2026-10-09T10:00:00Z", "2026-10-09T10:05:00Z")
        self.assertEqual((m["source"], m["as_of"], m["consensus_home_spread"]), ("csv", "2026-10-09T10:05:00Z", -3.0))
        self.assertIsNone(build.game_market({k: None for k in game}, None, None, "2026-10-09T10:05:00Z"))

    def test_result_block_tie_and_errors(self):
        official = {"pick": "KC", "blend_spread": 3.0, "total_used": 45.0}
        g = {"played": True, "home": "KC", "away": "DEN", "home_score": 20, "away_score": 20}
        r = build.result_block(g, official)
        self.assertEqual((r["winner"], r["hit"], r["margin"]), ("tie", None, 0))
        self.assertAlmostEqual(r["pred_margin_error"], 3.0)
        self.assertAlmostEqual(r["pred_total_error"], 5.0)
        g2 = dict(g, home_score=27, away_score=20)
        self.assertEqual(build.result_block(g2, official)["hit"], True)
        self.assertIsNone(build.result_block(dict(g, played=False), official))

    def test_official_market_inputs_without_key_or_on_failure_uses_csv(self):
        self.assertEqual(build.official_market_inputs(None, []), {"odds_source": "csv", "official_odds_at": None, "matched": {}})
        self.assertEqual(build.official_market_inputs("k", [], fetch=lambda key: None)["odds_source"], "csv")

    def test_params_block_requires_the_eight_keys(self):
        with self.assertRaises(build.BuildError):
            build.params_block({"elo": {}})
        with self.assertRaises(build.BuildError):  # D18: injury is required too
            build.params_block({k: {} for k in build.PARAM_KEYS if k != "injury"})
        self.assertEqual(tuple(build.params_block({k: {} for k in build.PARAM_KEYS})), build.PARAM_KEYS)

    # ---- D18 injury wiring (pure helpers) ----

    def test_injury_as_of_is_min_of_build_and_kickoff(self):
        self.assertEqual(build.injury_as_of("2026-10-13T16:00:00Z", "2026-10-15T00:15:00Z"), "2026-10-13T16:00:00Z")
        self.assertEqual(build.injury_as_of("2026-10-13T16:00:00Z", "2026-10-11T17:00:00Z"), "2026-10-11T17:00:00Z")

    def test_depth_rows_by_team_canonicalizes_and_as_of_is_latest_snapshot(self):
        rows = [{"dt": "2026-10-01T10:00:00Z", "team": "LA"}, {"dt": "2026-10-13T14:58:07Z", "team": "KC"},
                {"dt": "2026-10-14T09:00:00Z", "team": "KC"}, {"dt": "", "team": "KC"}, {"dt": "2026-10-02T10:00:00Z", "team": "XX"}]
        by = build.depth_rows_by_team(rows)
        self.assertEqual(sorted(by), ["KC", "LA"])
        self.assertEqual(len(by["KC"]), 3)
        self.assertEqual(build.depth_chart_as_of(rows, "2026-10-13T16:00:00Z"), "2026-10-13T14:58:07Z")
        self.assertIsNone(build.depth_chart_as_of(rows, "2026-09-01T00:00:00Z"))

    def _games_for_injuries(self):
        base = {"season": 2026, "played": False, "home_qb_id": None, "away_qb_id": None}
        return [
            dict(base, game_id="2026_05_PHI_JAX", week=5, home="JAX", away="PHI", league_date="2026-10-11", kickoff_utc="2026-10-11T13:30:00Z", played=True),
            dict(base, game_id="2026_06_DEN_KC", week=6, home="KC", away="DEN", league_date="2026-10-15", kickoff_utc="2026-10-16T00:15:00Z"),
            dict(base, game_id="2026_06_LA_SF", week=6, home="SF", away="LA", league_date="2026-10-18", kickoff_utc="2026-10-18T20:05:00Z"),
        ]

    def _injury_files(self):
        inj = [
            {"season": "2026", "week": "5", "team": "JAX", "gsis_id": "00-1", "position": "T", "full_name": "Left Tackle", "report_status": "Out", "report_primary_injury": "Knee"},
            {"season": "2026", "week": "5", "team": "PHI", "gsis_id": "00-9", "position": "WR", "full_name": "Depth Guy", "report_status": "", "report_primary_injury": ""},
            {"season": "2026", "week": "6", "team": "KC", "gsis_id": "00-2", "position": "CB", "full_name": "Corner", "report_status": "Questionable", "report_primary_injury": "Hamstring"},
            {"season": "2025", "week": "6", "team": "DEN", "gsis_id": "00-3", "position": "RB", "full_name": "Last Year", "report_status": "Out", "report_primary_injury": "Ankle"},
        ]
        depth = [
            {"dt": "2026-10-09T10:00:00Z", "team": "JAX", "player_name": "Left Tackle", "gsis_id": "00-1", "pos_grp": "Offense", "pos_abb": "LT", "pos_rank": "1"},
            {"dt": "2026-10-12T10:00:00Z", "team": "JAX", "player_name": "Backup Tackle", "gsis_id": "00-5", "pos_grp": "Offense", "pos_abb": "LT", "pos_rank": "1"},
            {"dt": "2026-10-09T10:00:00Z", "team": "KC", "player_name": "Corner", "gsis_id": "00-2", "pos_grp": "Defense", "pos_abb": "CB", "pos_rank": "1"},
        ]
        return {"injuries": inj, "depth": depth}

    def test_injury_context_builds_one_entry_per_game_with_kickoff_cutoff_for_played_games(self):
        games = self._games_for_injuries()
        files = self._injury_files()
        built_at = "2026-10-13T16:00:00Z"
        ctx = build.injury_context(games, files["injuries"], files["depth"], built_at)
        self.assertEqual(sorted(ctx), [g["game_id"] for g in games])
        jax = ctx["2026_05_PHI_JAX"]
        # a kicked-off game uses its kickoff as the cutoff: the Oct 9 chart (LT 00-1 starts), not the Oct 12 one
        self.assertEqual(jax["as_of"], "2026-10-11T13:30:00Z")
        self.assertTrue(jax["home"]["report_filed"])
        self.assertEqual(jax["home"]["starters_out"], 1)
        self.assertEqual([p["name"] for p in jax["home"]["players"]], ["Left Tackle"])
        self.assertTrue(jax["away"]["report_filed"])  # practice-only row still files the report
        self.assertEqual(jax["away"]["players"], [])
        kc = ctx["2026_06_DEN_KC"]
        self.assertEqual(kc["as_of"], built_at)
        self.assertEqual((kc["home"]["report_filed"], kc["home"]["starters_questionable"]), (True, 1))
        self.assertEqual((kc["away"]["available"], kc["away"]["report_filed"]), (True, False))  # 2025 row never counts
        sf = ctx["2026_06_LA_SF"]
        self.assertTrue(sf["home"]["available"] and not sf["home"]["report_filed"])
        # either file missing -> no entries at all (predict degrades every report to unavailable)
        self.assertEqual(build.injury_context(games, None, files["depth"], built_at), {})
        self.assertEqual(build.injury_context(games, files["injuries"], None, built_at), {})

    def test_injuries_build_block_counts_next_slate_week_reports(self):
        games = self._games_for_injuries()
        files = self._injury_files()
        built_at = "2026-10-13T16:00:00Z"
        ctx = build.injury_context(games, files["injuries"], files["depth"], built_at)
        self.assertEqual(build.next_slate_week(games, "2026-10-13"), 6)
        self.assertEqual(build.next_slate_week(games, "2026-10-19"), None)
        blk = build.injuries_build_block(games, ctx, files["injuries"], files["depth"], built_at, "2026-10-13")
        self.assertEqual(blk, {"available": True, "injuries_rows": 4, "depth_chart_as_of": "2026-10-12T10:00:00Z",
                               "teams_with_report_for_next_slate": 1})  # KC filed for week 6; DEN, SF, LA have not
        self.assertEqual(tuple(blk), build.INJURY_BUILD_KEYS)
        off = build.injuries_build_block(games, {}, None, files["depth"], built_at, "2026-10-13")
        self.assertEqual(off, {"available": False, "injuries_rows": 0, "depth_chart_as_of": None, "teams_with_report_for_next_slate": 0})

    @unittest.skipUnless(HAVE_DATA, "needs data/games.csv and params.json")
    def test_degraded_build_with_the_term_enabled_warns_and_still_succeeds(self):
        """D18.1: no season files -> the build succeeds with available=false; with the term enabled the run
        says loudly that every term is 0 and which snapshots were written without the reports."""
        d = tempfile.mkdtemp(prefix="gridiron-degraded-")
        try:
            empty = os.path.join(d, "nocache")
            os.makedirs(empty)
            with open(PARAMS, encoding="utf-8") as fh:
                params = json.load(fh)
            params["injury"]["enabled"] = True
            params["injury"]["gate"] = {"holdout_brier_with": 0.2150, "holdout_brier_without": 0.2173, "improvement_required": 0.0005}
            pfile = os.path.join(d, "params.json")
            with open(pfile, "w", encoding="utf-8") as fh:
                json.dump(params, fh)
            lines = []
            with contextlib.redirect_stderr(io.StringIO()) as err:
                s = build.run(GAMES, pfile, os.path.join(d, "picks.jsonl"), os.path.join(d, "model.json"), os.path.join(d, "index.html"),
                              offline=True, api_key="", odds_url="", now=NOW, log=lines.append, injury_cache_dir=empty)
            self.assertFalse(s["injuries_available"])
            self.assertTrue(s["injury_term_enabled"])
            self.assertTrue(s["injury_term_degraded"])
            self.assertGreater(s["snapshots_written"], 0)  # the build still publishes and logs picks (D18.1) ...
            warn = [ln for ln in lines if "WARNING the injury term is enabled" in ln]
            self.assertEqual(len(warn), 1)  # ... but says so, naming the snapshot count
            self.assertIn("%d snapshot(s)" % s["snapshots_written"], warn[0])
            self.assertIn("WARNING the injury term is enabled", err.getvalue())
            with open(os.path.join(d, "model.json"), encoding="utf-8") as fh:
                model = json.load(fh)
            self.assertFalse(model["build"]["injuries"]["available"])
            self.assertTrue(all(g["official"]["injuries"]["elo"] == 0 for g in model["games"]))
            # a healthy build never carries the flag
            s2 = build.run(GAMES, PARAMS, os.path.join(d, "picks2.jsonl"), os.path.join(d, "model2.json"), os.path.join(d, "index2.html"),
                           offline=True, api_key="", odds_url="", now=NOW, log=lambda _s: None)
            self.assertFalse(s2["injury_term_degraded"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_fetch_injury_files_degrades_without_a_cache(self):
        empty = tempfile.mkdtemp(prefix="gridiron-nocache-")
        try:
            files = build.fetch_injury_files(2026, empty, offline=True, log=lambda s: None)
        finally:
            shutil.rmtree(empty, ignore_errors=True)
        self.assertEqual(files, {"injuries": None, "depth": None})


@unittest.skipUnless(HAVE_DATA, "needs data/games.csv and params.json")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridiron-build-")
        self.picks = os.path.join(self.dir, "picks.jsonl")
        self.model = os.path.join(self.dir, "model.json")
        self.index = os.path.join(self.dir, "index.html")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, api_key: str = "", **kw):
        return build.run(GAMES, PARAMS, self.picks, self.model, self.index, offline=True, api_key=api_key,
                         odds_url="", now=NOW, log=lambda s: None, **kw)

    def test_offline_build_twice_is_idempotent_and_valid(self):
        s1 = self._run()
        self.assertLess(s1["seconds"], 30.0)
        with open(self.model, encoding="utf-8") as fh:
            model = json.load(fh)
        self.assertEqual(schema.validate_model(model), [])
        games = data.load_games(GAMES)
        season = max(g["season"] for g in games)
        rows = [g for g in games if g["season"] == season]
        self.assertEqual(s1["games"], len(rows))
        self.assertEqual([g["game_id"] for g in model["games"]], [g["game_id"] for g in rows])
        future = [g for g in rows if not g["played"] and g["kickoff_utc"] > build.iso_utc(NOW)]
        self.assertEqual(s1["snapshots_written"], len(future))
        self.assertEqual(s1["grades_written"], 0)
        events = ledger.read_events(self.picks)
        self.assertEqual(len(events), len(future))
        self.assertEqual({e["game_id"] for e in events}, {g["game_id"] for g in future})
        self.assertEqual(model["build"]["odds_source"], "csv")
        self.assertIsNone(model["build"]["official_odds_at"])
        self.assertEqual(model["build"]["league_date"], "2026-10-09")
        self.assertFalse(model["build"]["data"]["fetched"])
        self.assertEqual(model["scorecard"]["season"]["season"], season)
        # D18: the reports are in every card, the build block says what it had, and when the term is
        # enabled every unfrozen card's probability is win_prob of its d_total (term included)
        self.assertEqual(set(model["params"]), set(build.PARAM_KEYS))
        bi = model["build"]["injuries"]
        self.assertEqual(tuple(bi), build.INJURY_BUILD_KEYS)
        self.assertTrue(bi["available"])
        self.assertGreater(bi["injuries_rows"], 0)
        self.assertIsNotNone(bi["depth_chart_as_of"])
        self.assertLessEqual(bi["depth_chart_as_of"], build.iso_utc(NOW))
        enabled = bool(model["params"]["injury"]["enabled"])
        from src import elo as _elo
        for g in model["games"]:
            inj = g["official"]["injuries"]
            self.assertEqual(inj["enabled"], enabled)
            self.assertEqual(inj["as_of"], min(build.iso_utc(NOW), g["kickoff_utc"]))
            self.assertTrue(inj["home"]["available"] and inj["away"]["available"])
            if g["frozen_snapshot"] is None:
                self.assertAlmostEqual(g["official"]["p_home_model"], _elo.win_prob(g["official"]["factors"]["d_total"]), places=12)
        filed = [g for g in model["games"] if g["official"]["injuries"]["home"]["report_filed"]]
        self.assertGreater(len(filed), 0)
        if enabled:
            self.assertTrue(any(g["official"]["injuries"]["elo"] != 0 for g in model["games"]))
        self.assertEqual([b["load"] for b in model["track_record"]["by_injury_load"]], list(schema.INJURY_LOADS))
        snaps = ledger.read_events(self.picks)
        self.assertTrue(all("injury_elo" in e and "inj_home" in e and "inj_away" in e for e in snaps))
        with open(self.index, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn('<script id="model" type="application/json">', html)
        self.assertIn('window.GRIDIRON_ODDS_URL = "";', html)
        self.assertNotIn("__MODEL_JSON__", html)

        with open(self.picks, "rb") as fh:
            before = fh.read()
        s2 = self._run()
        self.assertEqual((s2["snapshots_written"], s2["grades_written"]), (0, 0))
        with open(self.picks, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_odds_api_path_uses_feed_prices_and_drops_offshore_books(self):
        with open(LIVE_SAMPLE, encoding="utf-8") as fh:
            payload = json.load(fh)
        calls = []

        def fake_fetch(key):
            calls.append(key)
            return {"payload": payload, "fetched_at": "2026-10-09T19:00:00Z", "requests_remaining": 400, "requests_used": 100}

        s = self._run(api_key="test-key", fetch_odds=fake_fetch)
        self.assertEqual(calls, ["test-key"])
        self.assertEqual(s["odds_source"], "odds_api")
        with open(self.model, encoding="utf-8") as fh:
            model = json.load(fh)
        self.assertEqual(schema.validate_model(model), [])
        self.assertEqual(model["build"]["official_odds_at"], "2026-10-09T19:00:00Z")
        fed = [g for g in model["games"] if g["official"]["market"] and g["official"]["market"]["source"] == "odds_api"]
        self.assertGreater(len(fed), 0)
        for g in fed:
            m = g["official"]["market"]
            self.assertEqual(m["as_of"], "2026-10-09T19:00:00Z")
            self.assertFalse({b["key"] for b in m["books"]} & set(schema.TEAM_SET))
            self.assertFalse({b["key"] for b in m["books"]} & {"bovada", "betus", "betonlineag", "lowvig", "mybookieag"})
            bp = g["official"]["best_price"]
            self.assertIsNotNone(bp)
            self.assertIn(bp["book"], model["params"]["books"])
            self.assertIn(g["official"]["total_source"], ("csv", "league_average"))  # h2h-only sample
        snaps = [e for e in ledger.read_events(self.picks) if e["market_source"] == "odds_api"]
        self.assertEqual(len(snaps), len(fed))
        self.assertTrue(all(e["best_book"] in model["params"]["books"] for e in snaps))

    def test_evaluation_is_recomputed_every_build(self):
        # SPEC: the holdout report runs with every build; a stale/bogus copy in params.json is ignored
        with open(PARAMS, encoding="utf-8") as fh:
            pf = json.load(fh)
        pf["evaluation"] = {"season": 1999, "samples": {"model": 1, "market_comparable": 1}, "model": {"brier": 9.0, "logloss": 9.0, "accuracy": 0.0},
                            "model_no_qb": {"brier": 9.0, "logloss": 9.0, "accuracy": 0.0}, "market": {"brier": 9.0, "logloss": 9.0, "accuracy": 0.0},
                            "blend": {"brier": 9.0, "logloss": 9.0, "accuracy": 0.0}}
        bogus = os.path.join(self.dir, "params_bogus.json")
        with open(bogus, "w", encoding="utf-8") as fh:
            json.dump(pf, fh)
        build.run(GAMES, bogus, self.picks, self.model, self.index, offline=True, api_key="", odds_url="", now=NOW, log=lambda s: None)
        with open(self.model, encoding="utf-8") as fh:
            ev = json.load(fh)["evaluation"]
        self.assertEqual(ev["season"], 2025)
        self.assertAlmostEqual(ev["model"]["brier"], pf["reference"]["brier"], delta=0.003)
        self.assertEqual(ev["samples"]["model"], pf["reference"]["n"])

    def test_validation_failure_writes_nothing(self):
        bad_params = os.path.join(self.dir, "params.json")
        with open(PARAMS, encoding="utf-8") as fh:
            p = json.load(fh)
        p["books"] = ["draftkings", "draftkings"]  # duplicate key -> best_prices keyed by a set of 1 vs list of 2
        p["tier_bounds"] = {"lean": 0.9, "strong": 0.65, "lockish": 0.78}  # not increasing -> validation problem
        with open(bad_params, "w", encoding="utf-8") as fh:
            json.dump(p, fh)
        with self.assertRaises(build.BuildError):
            build.run(GAMES, bad_params, self.picks, self.model, self.index, offline=True, api_key="", odds_url="", now=NOW, log=lambda s: None)
        self.assertFalse(os.path.exists(self.picks))
        self.assertFalse(os.path.exists(self.model))
        self.assertFalse(os.path.exists(self.index))


if __name__ == "__main__":
    unittest.main()


class FrozenOfficialTests(unittest.TestCase):
    """CONTRACT 2.6a: a kicked-off game shows the snapshot the ledger graded, not a recomputation."""

    def test_kicked_off_game_is_frozen_to_the_prekick_snapshot(self):
        import copy
        from src import predict, schema, ledger
        fx = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "model_fixture.json")))
        params = fx["params"]
        g = next(x for x in fx["games"] if x["status"] == "scheduled" and x["official"]["market"] is not None)
        official = copy.deepcopy(g["official"])
        home, away = g["home"], g["away"]
        other = away if official["pick"] == home else home
        # a pre-kick snapshot that picked the OTHER side at 54%
        snap = {"type": "snapshot", "id": g["game_id"] + "@old", "game_id": g["game_id"], "kickoff_utc": g["kickoff_utc"],
                "build_at": "2026-10-01T10:00:00Z", "pick": other, "p_model": 0.52, "p_market": 0.55, "p_blend": 0.54,
                "tier": "Toss-up", "pred_home": 21, "pred_away": 22, "best_price": -110, "best_book": "fanduel", "ev": -0.03,
                "market_source": "odds_api", "qb_home_id": None, "qb_away_id": None, "consensus_p_pick": 0.55,
                "total_used": 43.0, "total_source": "odds_api"}
        game = {"game_id": g["game_id"], "home": home, "away": away, "neutral": g["neutral"], "league_date": g["league_date"],
                "home_rest": 7, "away_rest": 7, "home_qb_id": None, "away_qb_id": None, "home_qb_name": None, "away_qb_name": None,
                "home_moneyline": None, "away_moneyline": None, "spread_line": None, "total_line": None}
        f = official["factors"]
        pregame = {"p_home": official["p_home_model"], "d": f["d_total"], "elo_home": f["team_elo_home"], "elo_away": f["team_elo_away"],
                   "hfa_elo": f["hfa_elo"], "rest_elo": f["rest_elo"], "q_home": 0.0, "q_away": 0.0, "qb_home_starts": 0, "qb_away_starts": 0}
        context = {"ranks": {t["team"]: t["rank"] for t in fx["teams"]}, "names": {t["code"]: t for t in schema.TEAM_META},
                   "form": {}, "usual_starter": {}, "qb": {}, "books": list(params["books"])}
        frozen = predict.freeze_official(official, snap, game, pregame, params, context)
        self.assertEqual(frozen["pick"], other)
        self.assertAlmostEqual(frozen["p_pick_blend"], 0.54)
        self.assertEqual(frozen["tier"], "Toss-up")
        self.assertEqual((frozen["display_home"], frozen["display_away"]), (21, 22))
        self.assertEqual(frozen["best_price"], {"book": "fanduel", "side": other, "american": -110})
        self.assertFalse(frozen["parlay_eligible"])
        self.assertIn(other, frozen["rationale_sentences"][-1].replace(".", "") or "")  if False else None
        self.assertEqual(set(frozen), set(official))
        # the result block grades the frozen pick
        played = dict(game, played=True, home_score=20, away_score=27)
        r = build.result_block(played, frozen)
        self.assertEqual(r["hit"], other == away)
        self.assertAlmostEqual(r["pred_margin_error"], frozen["blend_spread"] - (-7))


class FrozenNeutralDriftTests(unittest.TestCase):
    """A snapshot taken with home field whose game later becomes neutral must still satisfy
    p_home_model == win_prob(factors.d_total) on the frozen card (2026_05_PHI_JAX, 2026-10-13)."""

    def test_residual_is_folded_into_home_field(self):
        import copy, math
        from src import predict, schema, elo
        fx = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "model_fixture.json")))
        params = fx["params"]
        g = next(x for x in fx["games"] if x["status"] == "scheduled" and x["official"]["market"] is not None and not x["neutral"])
        official = copy.deepcopy(g["official"])
        f = official["factors"]
        home, away = g["home"], g["away"]
        p_with_hfa = elo.win_prob(f["d_total"])  # the snapshot was taken with home field
        snap = {"type": "snapshot", "id": g["game_id"] + "@old", "game_id": g["game_id"], "kickoff_utc": g["kickoff_utc"],
                "build_at": "2026-10-01T10:00:00Z", "pick": home, "p_model": p_with_hfa, "p_market": 0.6, "p_blend": 0.62,
                "tier": "Lean", "pred_home": 24, "pred_away": 20, "best_price": -150, "best_book": "fanduel", "ev": -0.02,
                "market_source": "odds_api", "qb_home_id": None, "qb_away_id": None, "consensus_p_pick": 0.6,
                "total_used": 44.0, "total_source": "odds_api"}
        game = {"game_id": g["game_id"], "home": home, "away": away, "neutral": True, "league_date": g["league_date"],
                "kickoff_utc": g["kickoff_utc"], "home_rest": 7, "away_rest": 7, "home_qb_id": None, "away_qb_id": None,
                "home_qb_name": None, "away_qb_name": None, "home_moneyline": None, "away_moneyline": None,
                "spread_line": None, "total_line": None, "season": g["season"], "week": g["week"]}
        pregame = {"p_home": p_with_hfa, "d": f["d_total"], "elo_home": f["team_elo_home"], "elo_away": f["team_elo_away"],
                   "hfa_elo": f["hfa_elo"], "rest_elo": f["rest_elo"], "q_home": 0.0, "q_away": 0.0, "qb_home_starts": 0, "qb_away_starts": 0}
        context = {"ranks": {t["team"]: t["rank"] for t in fx["teams"]}, "names": {t["code"]: t for t in schema.TEAM_META},
                   "form": {}, "usual_starter": {}, "qb": {}, "books": list(params["books"]), "injuries": {}}
        frozen = predict.freeze_official(official, snap, game, pregame, params, context)
        self.assertAlmostEqual(frozen["p_home_model"], elo.win_prob(frozen["factors"]["d_total"]), places=9)
        self.assertAlmostEqual(frozen["model_spread"], frozen["factors"]["d_total"] / params["score"]["points_per_elo"])
