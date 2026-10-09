"""Tests for src/ledger.py (D10, CONTRACT 2.12 / 4 / 6.6)."""

import copy
import json
import os
import tempfile
import unittest

from src import ledger
from src.schema import TIERS, _check_track_record

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "picks_fixture.jsonl")
W = 0.35


def make_game(game_id, kickoff, home, away, pick_home, p_home_model, p_home_market, tier,
              disp_home, disp_away, scores=None, close=(-110, -110), spread_line=0.0,
              total_line=44.0, best_price=-110, market=True):
    """A model.json games[] entry extended with the CSV close fields the ledger reads."""
    p_blend = W * p_home_model + (1 - W) * p_home_market if market else p_home_model
    pick = home if pick_home else away
    p_pick = p_blend if pick_home else 1 - p_blend
    best = None if best_price is None else {"book": "draftkings", "side": pick, "american": best_price}
    official = {
        "pick": pick,
        "p_home_model": p_home_model,
        "p_home_market": p_home_market if market else None,
        "p_home_blend": p_blend,
        "p_pick_blend": p_pick,
        "tier": tier,
        "model_spread": float(disp_home - disp_away),
        "expected_home": float(disp_home),
        "expected_away": float(disp_away),
        "display_home": disp_home,
        "display_away": disp_away,
        "total_used": total_line,
        "total_source": "csv",
        "market": {"source": "odds_api", "as_of": "2026-09-09T14:00:00Z", "consensus_p_home": p_home_market,
                   "consensus_total": total_line, "consensus_home_spread": -spread_line, "n_books": 1,
                   "books": [{"key": "draftkings", "home_ml": -110, "away_ml": -110,
                              "home_spread": -spread_line, "total": total_line}]} if market else None,
        "best_prices": {"draftkings": best},
        "best_price": best,
        "estimated_ev_per_dollar": None if best is None else p_pick * ledger._decimal_profit(best_price) - (1 - p_pick),
        "factors": {"qb_home": {"id": "00-" + home, "name": "QB " + home},
                    "qb_away": {"id": "00-" + away, "name": "QB " + away}},
        "rationale": [], "rationale_sentences": [], "edge": None, "parlay_eligible": True,
    }
    game = {
        "game_id": game_id, "season": int(game_id[:4]), "game_type": "REG", "week": int(game_id[5:7]),
        "league_date": kickoff[:10], "kickoff_utc": kickoff, "time_known": True,
        "away": away, "home": home, "neutral": False,
        "status": "final" if scores else "scheduled",
        "availability": {"starter_home": True, "starter_away": True, "market": market, "csv_lines": close is not None},
        "official": official, "result": None,
        "home_moneyline": None if close is None else close[0],
        "away_moneyline": None if close is None else close[1],
        "spread_line": spread_line, "total_line": total_line,
    }
    if scores:
        hs, as_ = scores
        margin = hs - as_
        game["result"] = {"home_score": hs, "away_score": as_,
                          "winner": home if margin > 0 else (away if margin < 0 else "tie"),
                          "margin": margin, "hit": None if margin == 0 else ((margin > 0) == pick_home),
                          "pred_margin_error": float(disp_home - disp_away - margin),
                          "pred_total_error": float(total_line - hs - as_)}
    return game


BUILD_1 = {"id": "20260909T140000Z-abc1234", "built_at": "2026-09-09T14:00:00Z"}
BUILD_2 = {"id": "20260911T140000Z-abc1234", "built_at": "2026-09-11T14:00:00Z"}
KICK = "2026-09-13T17:00:00Z"


def scheduled(**kw):
    base = dict(game_id="2026_01_KC_LAC", kickoff=KICK, home="LAC", away="KC", pick_home=False,
                p_home_model=0.44, p_home_market=0.40, tier="Lean", disp_home=21, disp_away=24)
    base.update(kw)
    return make_game(**base)


def final(scores=(17, 27), **kw):
    return scheduled(scores=scores, **kw)


class FixtureTests(unittest.TestCase):
    """The Codex-corrected 10-pick fixture: 6 hits at -110 => 6-4, 60%, +1.4545 units."""

    def setUp(self):
        self.events = ledger.read_events(FIXTURE)
        self.games = []
        for ev in self.events:
            if ev["type"] == "grade":
                snap = next(s for s in self.events if s.get("id") == ev["snapshot_id"])
                home = ev["game_id"].split("_")[3]
                self.games.append({"game_id": ev["game_id"], "season": int(ev["game_id"][:4]),
                                   "week": int(ev["game_id"][5:7]), "home": home,
                                   "spread_line": 1.0, "total_line": 44.0,
                                   "status": "final", "kickoff_utc": snap["kickoff_utc"]})
        self.out = ledger.reduce(self.events, self.games)
        self.tr = self.out["track_record"]

    def test_fixture_shape(self):
        self.assertEqual(sum(1 for e in self.events if e["type"] == "snapshot"), 10)
        self.assertEqual(sum(1 for e in self.events if e["type"] == "grade"), 10)
        self.assertTrue(all(e["schema_version"] == "1.0.0" for e in self.events))

    def test_overall_record(self):
        ov = self.tr["overall"]
        self.assertEqual((ov["n"], ov["hits"], ov["losses"], ov["pushes"]), (10, 6, 4, 0))
        self.assertAlmostEqual(ov["accuracy"], 0.6)
        self.assertEqual(ov["market_n"], 10)

    def test_units_positive_at_minus_110(self):
        u = self.tr["units"]["all"]
        self.assertEqual((u["n"], u["hits"], u["losses"]), (10, 6, 4))
        self.assertAlmostEqual(u["units"], 6 * (100 / 110) - 4, places=9)
        self.assertAlmostEqual(u["units"], 1.4545, places=4)
        self.assertAlmostEqual(u["roi"], 0.14545, places=4)
        self.assertAlmostEqual(u["mean_break_even"], 0.5238, places=4)
        self.assertAlmostEqual(self.tr["units"]["break_even_minus_110"], 110 / 210)
        self.assertAlmostEqual(self.tr["units"]["break_even_minus_110"], 0.5238, places=4)

    def test_ev_positive_subset(self):
        evp = self.tr["units"]["ev_positive"]
        n_pos = sum(1 for e in self.events if e["type"] == "snapshot" and e["ev"] is not None and e["ev"] >= 0)
        self.assertEqual(evp["n"], n_pos)
        self.assertLessEqual(evp["n"], self.tr["units"]["all"]["n"])

    def test_by_tier_and_by_week_carry_n(self):
        self.assertEqual([t["tier"] for t in self.tr["by_tier"]], list(TIERS))
        self.assertEqual(sum(t["n"] for t in self.tr["by_tier"]), 10)
        weeks = self.tr["by_week"]
        self.assertEqual([(w["season"], w["week"]) for w in weeks], [(2026, 1), (2026, 2)])
        self.assertEqual(weeks[-1]["cum_n"], 10)
        self.assertEqual(weeks[-1]["cum_hits"], 6)
        self.assertAlmostEqual(weeks[-1]["cum_accuracy"], 0.6)
        self.assertEqual(weeks[0]["cum_n"], weeks[0]["n"])
        self.assertEqual(weeks[-1]["market_cum_n"], 10)

    def test_calibration_all_low_sample_excluded(self):
        cal = self.tr["calibration"]
        self.assertEqual(len(cal["bins"]), 10)
        self.assertEqual(sum(b["n"] for b in cal["bins"]), cal["n"])
        self.assertEqual(cal["n"], 10)
        self.assertTrue(all(b["low_sample"] for b in cal["bins"]))
        self.assertIsNone(cal["error"])
        self.assertEqual(cal["error_n"], 0)

    def test_clv_and_score_error_have_n(self):
        self.assertEqual(self.tr["clv"]["n"], 10)
        self.assertIsNotNone(self.tr["clv"]["mean_pp"])
        self.assertEqual(self.tr["score_error"]["n"], 10)
        self.assertEqual(self.tr["score_error"]["market_n"], 10)

    def test_track_record_passes_schema_validator(self):
        problems = []
        _check_track_record(self.tr, problems)
        self.assertEqual(problems, [])

    def test_scorecard_season_matches_overall(self):
        sc = self.out["scorecard_season"]
        self.assertEqual(sc["season"], 2026)
        self.assertEqual((sc["n"], sc["hits"], sc["losses"], sc["pushes"]), (10, 6, 4, 0))
        self.assertAlmostEqual(sc["accuracy"], 0.6)
        self.assertEqual(set(self.out["grades"]), {g["game_id"] for g in self.games})

    def test_five_five_is_negative(self):
        events = copy.deepcopy(self.events)
        flipped = next(e for e in events if e["type"] == "grade" and e["hit"] is True)
        flipped["hit"] = False
        flipped["home_score"], flipped["away_score"] = flipped["away_score"], flipped["home_score"]
        flipped["margin"] = -flipped["margin"]
        flipped["result"] = "away" if flipped["result"] == "home" else "home"
        u = ledger.reduce(events, self.games)["track_record"]["units"]["all"]
        self.assertEqual((u["hits"], u["losses"]), (5, 5))
        self.assertAlmostEqual(u["units"], 5 * (100 / 110) - 5, places=9)
        self.assertLess(u["units"], 0)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_event_fields(self):
        ev = ledger.snapshot_event(scheduled(), BUILD_1)
        self.assertEqual(ev["type"], "snapshot")
        self.assertEqual(ev["id"], "2026_01_KC_LAC@" + BUILD_1["id"])
        self.assertEqual(ev["build_at"], BUILD_1["built_at"])
        self.assertEqual(ev["pick"], "KC")
        self.assertAlmostEqual(ev["p_model"], 0.56)  # pick-side probability
        self.assertAlmostEqual(ev["p_market"], 0.60)
        self.assertAlmostEqual(ev["consensus_p_pick"], 0.60)
        self.assertAlmostEqual(ev["p_blend"], 1 - (W * 0.44 + (1 - W) * 0.40))
        self.assertEqual((ev["pred_home"], ev["pred_away"]), (21, 24))
        self.assertEqual((ev["best_price"], ev["best_book"]), (-110, "draftkings"))
        self.assertEqual(ev["market_source"], "odds_api")
        self.assertEqual((ev["qb_home_id"], ev["qb_away"]), ("00-LAC", "QB KC"))

    def test_snapshot_none_for_final_or_post_kick(self):
        self.assertIsNone(ledger.snapshot_event(final(), BUILD_1))
        late = {"id": "x", "built_at": KICK}
        self.assertIsNone(ledger.snapshot_event(scheduled(), late))
        later = {"id": "x", "built_at": "2026-09-13T18:00:00Z"}
        self.assertIsNone(ledger.snapshot_event(scheduled(), later))

    def test_no_market_snapshot_has_nulls(self):
        ev = ledger.snapshot_event(scheduled(market=False, best_price=None), BUILD_1)
        self.assertIsNone(ev["p_market"])
        self.assertIsNone(ev["consensus_p_pick"])
        self.assertIsNone(ev["best_price"])
        self.assertIsNone(ev["ev"])
        self.assertIsNone(ev["market_source"])

    def test_rerun_adds_nothing(self):
        games = [scheduled()]
        first = ledger.append_snapshots([], games, BUILD_1)
        self.assertEqual(len(first), 1)
        self.assertEqual(ledger.append_snapshots(first, games, BUILD_1), [])
        self.assertEqual(ledger.append_snapshots(first, games, BUILD_2), [])  # same fields, later build

    def test_changed_fields_add_snapshot(self):
        first = ledger.append_snapshots([], [scheduled()], BUILD_1)
        second = ledger.append_snapshots(first, [scheduled(p_home_market=0.42)], BUILD_2)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["id"], "2026_01_KC_LAC@" + BUILD_2["id"])
        self.assertEqual(ledger.append_snapshots(first + second, [scheduled(p_home_market=0.42)], BUILD_2), [])

    def test_alias_build_snapshots(self):
        via_alias = ledger.build_snapshots([scheduled()], BUILD_1["id"], BUILD_1["built_at"], [])
        self.assertEqual(via_alias, ledger.append_snapshots([], [scheduled()], BUILD_1))


class GradeTests(unittest.TestCase):
    def test_grades_latest_prekick_of_two_snapshots(self):
        s1 = ledger.append_snapshots([], [scheduled()], BUILD_1)
        s2 = ledger.append_snapshots(s1, [scheduled(p_home_market=0.42)], BUILD_2)
        events = s1 + s2
        grades = ledger.append_grades(events, [final(p_home_market=0.42)], "2026-09-16T14:00:00Z")
        self.assertEqual(len(grades), 1)
        self.assertEqual(grades[0]["snapshot_id"], s2[0]["id"])
        self.assertEqual(grades[0]["result"], "away")
        self.assertEqual(grades[0]["margin"], -10)
        self.assertTrue(grades[0]["hit"])

    def test_post_kick_snapshot_ineligible(self):
        s1 = ledger.append_snapshots([], [scheduled()], BUILD_1)
        post = copy.deepcopy(s1[0])
        post["id"] = "2026_01_KC_LAC@post"
        post["build_at"] = "2026-09-13T17:30:00Z"  # after kickoff: must never be graded
        post["p_market"] = 0.99
        grades = ledger.append_grades(s1 + [post], [final()], "2026-09-16T14:00:00Z")
        self.assertEqual(grades[0]["snapshot_id"], s1[0]["id"])
        # a game with ONLY a post-kick snapshot gets no grade
        self.assertEqual(ledger.append_grades([post], [final()], "2026-09-16T14:00:00Z"), [])

    def test_grade_never_duplicated(self):
        events = ledger.append_snapshots([], [scheduled()], BUILD_1)
        g1 = ledger.append_grades(events, [final()], "2026-09-16T14:00:00Z")
        events = events + g1
        self.assertEqual(ledger.append_grades(events, [final()], "2026-09-17T14:00:00Z"), [])
        # a later snapshot change after grading never produces a second grade either
        self.assertEqual(ledger.append_grades(events, [final()], "2026-09-18T14:00:00Z"), [])

    def test_grade_values_close_and_clv(self):
        events = ledger.append_snapshots([], [scheduled()], BUILD_1)
        # closing KC -170 / LAC +150 => no-vig KC prob
        g = ledger.append_grades(events, [final(close=(150, -170))], "2026-09-16T14:00:00Z")[0]
        p_kc = ledger._no_vig_pair(150, -170)[1]
        self.assertAlmostEqual(g["close_p_pick"], p_kc)
        self.assertEqual(g["close_price_pick"], -170)
        self.assertAlmostEqual(g["clv_pp"], 100 * (p_kc - 0.60))
        self.assertGreater(g["clv_pp"], 0)  # the close moved toward the pick

    def test_grade_without_csv_close(self):
        events = ledger.append_snapshots([], [scheduled()], BUILD_1)
        g = ledger.append_grades(events, [final(close=None)], "2026-09-16T14:00:00Z")[0]
        self.assertIsNone(g["close_p_pick"])
        self.assertIsNone(g["close_price_pick"])
        self.assertIsNone(g["clv_pp"])
        self.assertTrue(g["hit"])

    def test_tie_is_push(self):
        events = ledger.append_snapshots([], [scheduled()], BUILD_1)
        g = ledger.append_grades(events, [final(scores=(20, 20))], "2026-09-16T14:00:00Z")[0]
        self.assertEqual(g["result"], "tie")
        self.assertIsNone(g["hit"])
        self.assertEqual(g["margin"], 0)

    def test_not_final_or_no_snapshot_not_graded(self):
        self.assertEqual(ledger.append_grades([], [final()], "2026-09-16T14:00:00Z"), [])
        events = ledger.append_snapshots([], [scheduled()], BUILD_1)
        self.assertEqual(ledger.append_grades(events, [scheduled()], "2026-09-16T14:00:00Z"), [])

    def test_alias_build_grades(self):
        events = ledger.append_snapshots([], [scheduled()], BUILD_1)
        self.assertEqual(ledger.build_grades([final()], events, "t"), ledger.append_grades(events, [final()], "t"))


class FileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "picks.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_is_empty(self):
        self.assertEqual(ledger.read_events(self.path), [])

    def test_existing_lines_byte_identical_after_build(self):
        snaps = ledger.append_snapshots([], [scheduled()], BUILD_1)
        ledger.write_events(self.path, snaps)
        with open(self.path, "rb") as fh:
            before = fh.read()
        self.assertTrue(before.endswith(b"\n"))
        events = ledger.read_events(self.path)
        self.assertEqual(events, snaps)
        # a later build: changed snapshot + grade; nothing already written changes
        new = ledger.append_snapshots(events, [scheduled(p_home_market=0.42)], BUILD_2)
        new += ledger.append_grades(events + new, [final(p_home_market=0.42)], "2026-09-16T14:00:00Z")
        ledger.write_events(self.path, new)
        with open(self.path, "rb") as fh:
            after = fh.read()
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(after.count(b"\n"), 3)
        self.assertEqual(len(ledger.read_events(self.path)), 3)
        # an identical rerun writes nothing at all
        events = ledger.read_events(self.path)
        rerun = ledger.append_snapshots(events, [final(p_home_market=0.42)], BUILD_2)
        rerun += ledger.append_grades(events, [final(p_home_market=0.42)], "2026-09-17T14:00:00Z")
        self.assertEqual(rerun, [])
        ledger.write_events(self.path, rerun)
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), after)

    def test_write_repairs_missing_trailing_newline_without_touching_bytes(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "snapshot", "id": "x"}))  # no newline
        ledger.append_events(self.path, [{"type": "grade", "snapshot_id": "x"}])
        with open(self.path, "r", encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        self.assertEqual(json.loads(lines[0]), {"type": "snapshot", "id": "x"})
        self.assertEqual(json.loads(lines[1])["type"], "grade")

    def test_malformed_line_fails_loudly(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"type":"snapshot"}\n\nnot json\n')
        with self.assertRaises(ValueError):
            ledger.read_events(self.path)


class ReduceTests(unittest.TestCase):
    def _graded(self, specs):
        """specs: list of (pick_home, p_home_market, scores, close, best_price, tier)."""
        events, games = [], []
        for i, (pick_home, pk, scores, close, best_price, tier) in enumerate(specs):
            gid = "2026_01_A%02d_H%02d" % (i, i)
            kw = dict(game_id=gid, kickoff=KICK, home="H%02d" % i, away="A%02d" % i, pick_home=pick_home,
                      p_home_model=pk, p_home_market=pk, tier=tier, disp_home=24, disp_away=20,
                      close=close, best_price=best_price)
            events += ledger.append_snapshots(events, [make_game(**kw)], BUILD_1)
            g = make_game(scores=scores, **kw)
            events += ledger.append_grades(events, [g], "2026-09-16T14:00:00Z")
            games.append(g)
        return events, games

    def test_ties_excluded_from_accuracy_and_units(self):
        events, games = self._graded([
            (True, 0.6, (24, 20), (-110, -110), -110, "Lean"),
            (True, 0.6, (20, 20), (-110, -110), -110, "Lean"),
            (True, 0.6, (17, 21), (-110, -110), -110, "Lean"),
        ])
        tr = ledger.reduce(events, games)["track_record"]
        ov = tr["overall"]
        self.assertEqual((ov["n"], ov["hits"], ov["losses"], ov["pushes"]), (3, 1, 1, 1))
        self.assertAlmostEqual(ov["accuracy"], 0.5)
        self.assertEqual(ov["market_n"], 2)
        u = tr["units"]["all"]
        self.assertEqual(u["pushes"], 1)
        self.assertAlmostEqual(u["units"], 100 / 110 - 1)
        self.assertEqual(tr["calibration"]["n"], 2)
        lean = tr["by_tier"][1]
        self.assertEqual((lean["tier"], lean["n"], lean["pushes"]), ("Lean", 3, 1))
        self.assertAlmostEqual(lean["accuracy"], 0.5)

    def test_market_pick_at_exactly_half_excluded(self):
        events, games = self._graded([
            (True, 0.5, (24, 20), (-110, -110), -110, "Toss-up"),
            (True, 0.7, (24, 20), (-110, -110), -110, "Strong"),
        ])
        ov = ledger.reduce(events, games)["track_record"]["overall"]
        self.assertEqual(ov["n"], 2)
        self.assertEqual(ov["market_n"], 1)
        self.assertEqual(ov["market_hits"], 1)

    def test_market_disagreeing_with_pick(self):
        # home pick, but the market favors away (p_market for the pick side < 0.5)
        pk = 0.40
        events, games = self._graded([(True, pk, (24, 20), (-110, -110), -110, "Toss-up")])
        # force the model to pick home despite the market
        snap = events[0]
        snap["pick"] = "H00"
        snap["p_market"] = pk
        events[1]["hit"] = True
        ov = ledger.reduce(events, games)["track_record"]["overall"]
        self.assertEqual((ov["hits"], ov["market_hits"], ov["market_n"]), (1, 0, 1))

    def test_unpriced_picks_excluded_from_units_only(self):
        events, games = self._graded([
            (True, 0.6, (24, 20), (-110, -110), None, "Lean"),
            (True, 0.6, (24, 20), (-110, -110), 120, "Lean"),
        ])
        tr = ledger.reduce(events, games)["track_record"]
        self.assertEqual(tr["overall"]["n"], 2)
        u = tr["units"]["all"]
        self.assertEqual(u["n"], 1)
        self.assertAlmostEqual(u["units"], 1.2)
        self.assertAlmostEqual(u["mean_break_even"], 1 / 2.2)

    def test_calibration_bin_with_ten_games_counts(self):
        specs = [(True, 0.62, (24, 20) if i < 7 else (20, 24), (-110, -110), -110, "Lean") for i in range(10)]
        specs += [(False, 0.35, (20, 24), (-110, -110), -110, "Lean")] * 3
        events, games = self._graded(specs)
        cal = ledger.reduce(events, games)["track_record"]["calibration"]
        self.assertEqual(cal["n"], 13)
        self.assertEqual(sum(b["n"] for b in cal["bins"]), 13)
        b6 = cal["bins"][6]
        self.assertEqual((b6["lo"], b6["hi"], b6["n"], b6["low_sample"]), (0.6, 0.7, 10, False))
        self.assertAlmostEqual(b6["mean_p"], 0.62)
        self.assertAlmostEqual(b6["hit_rate"], 0.7)
        b3 = cal["bins"][3]
        self.assertEqual((b3["n"], b3["low_sample"]), (3, True))
        self.assertAlmostEqual(cal["error"], abs(0.62 - 0.7))  # only the full bin contributes
        self.assertEqual(cal["error_n"], 10)
        empty = cal["bins"][0]
        self.assertEqual((empty["n"], empty["mean_p"], empty["hit_rate"], empty["low_sample"]), (0, None, None, True))

    def test_bin_edges(self):
        self.assertEqual(ledger._bin_index(0.0), 0)
        self.assertEqual(ledger._bin_index(0.1), 1)
        self.assertEqual(ledger._bin_index(0.3), 3)
        self.assertEqual(ledger._bin_index(0.7), 7)
        self.assertEqual(ledger._bin_index(0.999), 9)
        self.assertEqual(ledger._bin_index(1.0), 9)

    def test_score_error_and_clv(self):
        events, games = self._graded([(True, 0.6, (27, 17), (-150, 130), -110, "Lean")])
        g = games[0]
        g["spread_line"], g["total_line"] = 3.0, 47.0
        tr = ledger.reduce(events, games)["track_record"]
        se = tr["score_error"]
        self.assertEqual(se["n"], 1)
        self.assertAlmostEqual(se["margin_mae"], abs((24 - 20) - 10))
        self.assertAlmostEqual(se["total_mae"], abs(44 - 44))
        self.assertAlmostEqual(se["market_margin_mae"], abs(3.0 - 10))
        self.assertAlmostEqual(se["market_total_mae"], abs(47.0 - 44))
        clv = tr["clv"]
        self.assertEqual(clv["n"], 1)
        expected_pp = 100 * (ledger._no_vig_pair(-150, 130)[0] - 0.6)
        self.assertAlmostEqual(clv["mean_pp"], expected_pp)
        self.assertEqual(clv["share_toward_pick"], 0.0)

    def test_empty_ledger(self):
        out = ledger.reduce([], [])
        tr = out["track_record"]
        self.assertEqual(tr["overall"]["n"], 0)
        self.assertIsNone(tr["overall"]["accuracy"])
        self.assertEqual(tr["by_week"], [])
        self.assertEqual(len(tr["by_tier"]), 4)
        self.assertEqual(tr["calibration"]["n"], 0)
        self.assertIsNone(tr["calibration"]["error"])
        self.assertIsNone(tr["clv"]["mean_pp"])
        self.assertEqual(tr["units"]["all"]["n"], 0)
        self.assertIsNone(tr["units"]["all"]["roi"])
        self.assertIsNone(out["scorecard_season"]["season"])
        problems = []
        _check_track_record(tr, problems)
        self.assertEqual(problems, [])

    def test_reduce_accepts_dict_and_ignores_params(self):
        events, games = self._graded([(True, 0.6, (24, 20), (-110, -110), -110, "Lean")])
        by_id = {g["game_id"]: g for g in games}
        self.assertEqual(ledger.reduce(events, by_id, {"anything": 1}), ledger.reduce(events, games))

    def test_by_week_cumulative_resets_per_season(self):
        events, games = self._graded([(True, 0.6, (24, 20), (-110, -110), -110, "Lean")] * 2)
        games[1]["game_id"] = "2025_18_A01_H01"
        games[1]["season"], games[1]["week"] = 2025, 18
        for ev in events:
            if ev["game_id"] == "2026_01_A01_H01":
                ev["game_id"] = "2025_18_A01_H01"
        weeks = ledger.reduce(events, games)["track_record"]["by_week"]
        self.assertEqual([(w["season"], w["week"], w["cum_n"]) for w in weeks], [(2025, 18, 1), (2026, 1, 1)])


class OddsMathTests(unittest.TestCase):
    def test_decimal_profit_and_no_vig(self):
        self.assertAlmostEqual(ledger._decimal_profit(150), 1.5)
        self.assertAlmostEqual(ledger._decimal_profit(-120), 100 / 120)
        p_home, p_away = ledger._no_vig_pair(-150, 130)
        self.assertAlmostEqual(p_home + p_away, 1.0)
        self.assertGreater(p_home, 0.57)
        self.assertLess(p_home, 0.59)
        self.assertAlmostEqual(ledger._break_even(100 / 110), 110 / 210)
        with self.assertRaises(ValueError):
            ledger._decimal_profit(50)
        with self.assertRaises(ValueError):
            ledger._decimal_profit(0)


if __name__ == "__main__":
    unittest.main()
