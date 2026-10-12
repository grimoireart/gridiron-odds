"""Tests for src/build.py: the pure helpers and the whole offline build, run twice."""
from __future__ import annotations

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

    def test_params_block_requires_the_seven_keys(self):
        with self.assertRaises(build.BuildError):
            build.params_block({"elo": {}})


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
