"""Tests for src/predict.py: blend, scores, total source, prices/EV, the official
object (validated with schema.validate_model), rationale rules and number
provenance, edge chip priority, injury reports (D18: the 2.6b block, the
"injuries" sentence iff rule, the term in d_total, the edge chip, a frozen card)."""
import copy
import json
import os
import re
import unittest
from unittest import mock

from src import elo, injuries, predict, schema

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
NUM = re.compile(r"-?\d+(?:\.\d+)?")


def load_fixture():
    with open(os.path.join(FIX, "model_fixture.json"), encoding="utf-8") as fh:
        return json.load(fh)


MODEL_FIXTURE = load_fixture()
PARAMS = copy.deepcopy(MODEL_FIXTURE["params"])
AS_OF = "2026-10-10T14:02:41Z"


# ----------------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------------


def make_game(game_id, home, away, **over):
    season, week, _, _ = game_id.split("_")
    g = {
        "game_id": game_id, "season": int(season), "game_type": "REG", "week": int(week),
        "gameday": "2026-10-18", "gametime": "13:00", "weekday": "Sunday",
        "league_date": "2026-10-18", "kickoff_utc": "2026-10-18T17:00:00Z", "time_known": True,
        "home": home, "away": away, "home_raw": home, "away_raw": away,
        "location": "Home", "neutral": False,
        "home_score": None, "away_score": None, "played": False, "result": None, "total": None, "overtime": None,
        "home_rest": 7, "away_rest": 7,
        "home_moneyline": None, "away_moneyline": None, "spread_line": None, "total_line": None,
        "home_qb_id": "00-H" + home, "away_qb_id": "00-A" + away,
        "home_qb_name": "Home Starter " + home, "away_qb_name": "Away Starter " + away,
        "div_game": False, "roof": "outdoors", "surface": "grass", "stadium": "Field",
        "sort_key": (int(season), "2026-10-18", 0, "13:00", game_id),
    }
    g.update(over)
    return g


def make_pregame(elo_home, elo_away, hfa_elo=40.0, rest_elo=0.0, q_home=0.0, q_away=0.0,
                 starts_home=20, starts_away=20):
    d = (elo_home - elo_away) + hfa_elo + rest_elo + ((q_home or 0.0) - (q_away or 0.0))
    return {
        "p_home": 1.0 / (1.0 + 10 ** (-d / 400.0)), "d": d,
        "elo_home": elo_home, "elo_away": elo_away, "hfa_elo": hfa_elo, "rest_elo": rest_elo,
        "q_home": q_home, "q_away": q_away, "qb_home_starts": starts_home, "qb_away_starts": starts_away,
    }


def book(key, home_ml, away_ml, home_spread=-3.0, total=45.5):
    return {"key": key, "home_ml": home_ml, "away_ml": away_ml, "home_spread": home_spread, "total": total}


def no_vig_home(home_ml, away_ml):
    ih = predict.decimal_profit(home_ml)
    ia = predict.decimal_profit(away_ml)
    ph, pa = 1.0 / (1.0 + ih), 1.0 / (1.0 + ia)
    return ph / (ph + pa)


def make_market(books, source="odds_api", total=45.5, spread=-3.0):
    ps = [no_vig_home(b["home_ml"], b["away_ml"]) for b in books if b["home_ml"] is not None and b["away_ml"] is not None]
    return {
        "source": source, "as_of": AS_OF,
        "consensus_p_home": sum(ps) / len(ps) if ps else None,
        "consensus_total": total, "consensus_home_spread": spread,
        "n_books": len(books), "books": books,
    }


def make_context(form=None, usual=None, qb=None, ranks=None):
    if ranks is None:
        ranks = {t["team"]: t["rank"] for t in MODEL_FIXTURE["teams"]}
    return {
        "ranks": ranks,
        "names": {t["code"]: t for t in schema.TEAM_META},
        "form": form or {},
        "usual_starter": usual or {},
        "qb": qb or {},
        "books": list(PARAMS["books"]),
    }


def wrap(game, out):
    """A CONTRACT 2.5 games[] entry from predict_game's output."""
    return {
        "game_id": game["game_id"], "season": game["season"], "game_type": game["game_type"], "week": game["week"],
        "league_date": game["league_date"], "kickoff_utc": game["kickoff_utc"], "time_known": game["time_known"],
        "away": game["away"], "home": game["home"], "neutral": game["neutral"], "status": "scheduled",
        "availability": out["availability"], "official": out["official"], "result": None, "frozen_snapshot": None,
    }


def validate_games(entries, params=None):
    model = copy.deepcopy(MODEL_FIXTURE)
    model["games"] = entries
    if params is not None:
        model["params"] = copy.deepcopy(params)
    return schema.validate_model(model)


# ----------------------------------------------------------------------------
# Injury report builders (CONTRACT 2.6b shapes, hand-built so the tests do not depend on
# the CSV fixtures; src/injuries.py has its own tests)
# ----------------------------------------------------------------------------


def player(name, position, status, starter=True, injury="Knee", group=None):
    grp = group or injuries.pos_group(position)
    return {"gsis_id": "00-" + "".join(ch for ch in name.upper() if ch.isalpha())[:8], "name": name,
            "position": position, "pos_group": grp, "status": status, "injury": injury,
            "starter": starter, "covered_by_qb_term": grp == "QB"}


def make_report(*players, filed=True, qb_listed=None):
    ps = sorted(players, key=lambda p: (0 if p["starter"] else 1, injuries.STATUS_RANK[p["status"]], p["name"]))
    counted = [p for p in ps if p["starter"] and not p["covered_by_qb_term"]]
    return {
        "available": True, "report_filed": bool(filed or ps), "players": ps,
        "starters_out": sum(1 for p in counted if p["status"] == "Out"),
        "starters_doubtful": sum(1 for p in counted if p["status"] == "Doubtful"),
        "starters_questionable": sum(1 for p in counted if p["status"] == "Questionable"),
        "total_listed": len(ps), "qb_listed": qb_listed,
    }


def with_injuries(ctx, game, home_report, away_report, as_of=AS_OF):
    ctx = copy.deepcopy(ctx)
    ctx.setdefault("injuries", {})[game["game_id"]] = {"home": home_report, "away": away_report, "as_of": as_of}
    return ctx


def enabled_params():
    """params with the injury term switched on and its gate satisfied (CONTRACT 2.3)."""
    params = copy.deepcopy(PARAMS)
    params["injury"]["enabled"] = True
    params["injury"]["gate"] = {"holdout_brier_with": 0.2150, "holdout_brier_without": 0.2173, "improvement_required": 0.001}
    return params


DEN_REPORT = make_report(
    player("Courtland Sutton", "WR", "Out"), player("Garett Bolles", "LT", "Out"),
    player("Pat Surtain", "CB", "Out"), player("Brandon Jones", "S", "Out"),
    player("J.K. Dobbins", "RB", "Doubtful"), player("Zach Allen", "DE", "Questionable"),
    player("Backup Guard", "G", "Out", starter=False),
)
LAC_FILED_NONE = make_report(player("Deep Reserve", "LB", "Out", starter=False), player("Rotation End", "DE", "Questionable"))


def scenario_injuries(params=None):
    """LAC at DEN: Denver lists four starters Out and one Doubtful, the Chargers file a quiet
    report; no starter change, equal rest, small form moves, a market gap >= 0.08."""
    game = make_game("2026_07_LAC_DEN", "DEN", "LAC",
                     home_qb_id="00-NIX", home_qb_name="Bo Nix", away_qb_id="00-HERB", away_qb_name="Justin Herbert",
                     total_line=44.5, spread_line=-2.5, home_moneyline=-130, away_moneyline=110)
    pregame = make_pregame(1550.0, 1500.0, q_home=10.0, q_away=12.0, starts_home=30, starts_away=90)
    books = [book("draftkings", 120, -140), book("fanduel", 118, -138), book("betmgm", 125, -145), book("betrivers", 120, -142)]
    market = make_market(books, total=44.5, spread=2.5)
    context = make_context(
        form={"DEN": 12.0, "LAC": -8.0},
        usual={"DEN": {"id": "00-NIX", "name": "Bo Nix"}, "LAC": {"id": "00-HERB", "name": "Justin Herbert"}},
        qb={"00-NIX": {"q": 10.0, "name": "Bo Nix", "team": "DEN", "starts": 30, "last_start_date": "2026-10-11"},
            "00-HERB": {"q": 12.0, "name": "Justin Herbert", "team": "LAC", "starts": 90, "last_start_date": "2026-10-11"}},
    )
    context = with_injuries(context, game, DEN_REPORT, LAC_FILED_NONE)
    return game, pregame, market, context, (params or PARAMS)


# ----------------------------------------------------------------------------
# Scenarios shared by several tests
# ----------------------------------------------------------------------------


def scenario_starter_change():
    """KC at DEN: Denver's listed starter differs from the usual one; KC is on a run."""
    game = make_game("2026_06_KC_DEN", "DEN", "KC",
                     home_qb_id="00-STID", home_qb_name="Jarrett Stidham",
                     away_qb_id="00-MAHO", away_qb_name="Patrick Mahomes",
                     total_line=46.5, spread_line=-6.5, home_moneyline=130, away_moneyline=-150)
    pregame = make_pregame(1520.0, 1660.0, q_home=-20.0, q_away=60.0, starts_home=5, starts_away=120)
    # the market is far less sure of KC than the model (~0.60 vs ~0.75): market sentence + edge chip
    books = [book("draftkings", 130, -150), book("fanduel", 128, -148), book("betmgm", 135, -155),
             book("betrivers", 130, -152), book("bovada", 132, -150)]
    market = make_market(books, total=46.5, spread=6.5)
    context = make_context(
        form={"DEN": -12.0, "KC": 45.5},
        usual={"DEN": {"id": "00-NIX", "name": "Bo Nix"}, "KC": {"id": "00-MAHO", "name": "Patrick Mahomes"}},
        qb={"00-NIX": {"q": 15.0, "name": "Bo Nix", "team": "DEN", "starts": 40, "last_start_date": "2026-10-11"},
            "00-STID": {"q": -20.0, "name": "Jarrett Stidham", "team": "DEN", "starts": 5, "last_start_date": "2024-12-01"},
            "00-MAHO": {"q": 60.0, "name": "Patrick Mahomes", "team": "KC", "starts": 120, "last_start_date": "2026-10-11"}},
    )
    return game, pregame, market, context


def scenario_neutral_no_market():
    """SPEC score test: Elo gap 0, neutral site, no market, no total -> league average 44."""
    params = copy.deepcopy(PARAMS)
    params["score"]["league_average_total"] = 44.0
    game = make_game("2026_06_PHI_JAX", "JAX", "PHI", location="Neutral", neutral=True,
                     home_qb_id="00-LAWR", home_qb_name="Trevor Lawrence",
                     away_qb_id="00-HURT", away_qb_name="Jalen Hurts")
    pregame = make_pregame(1600.0, 1600.0, hfa_elo=0.0, q_home=10.0, q_away=10.0)
    context = make_context(usual={"JAX": {"id": "00-LAWR", "name": "Trevor Lawrence"}, "PHI": {"id": "00-HURT", "name": "Jalen Hurts"}})
    return game, pregame, None, context, params


def scenario_csv_market():
    """CSV-sourced market with a rest gap (home 10 days, away 6)."""
    game = make_game("2026_06_CLE_NYJ", "NYJ", "CLE", home_rest=10, away_rest=6,
                     home_moneyline=-150, away_moneyline=130, spread_line=3.0, total_line=44.5)
    pregame = make_pregame(1450.0, 1430.0, rest_elo=8.0, q_home=-5.0, q_away=-8.0)
    market = {
        "source": "csv", "as_of": AS_OF, "consensus_p_home": no_vig_home(-150, 130),
        "consensus_total": 44.5, "consensus_home_spread": -3.0, "n_books": 1,
        "books": [{"key": "csv", "home_ml": -150, "away_ml": 130, "home_spread": -3.0, "total": 44.5}],
    }
    return game, pregame, market, make_context()


def scenario_first_start_partial_books():
    """Home starter's first start on record; only two default books price the game,
    at the same price (tie -> first in params.books); away form collapse."""
    game = make_game("2026_06_CHI_GB", "GB", "CHI",
                     home_qb_id="00-NEWQB", home_qb_name="Rookie Arm",
                     away_qb_id="00-CALE", away_qb_name="Caleb Williams", total_line=43.0)
    pregame = make_pregame(1600.0, 1480.0, q_home=-50.0, q_away=12.0, starts_home=0, starts_away=30)
    books = [book("fanduel", -115, -105), book("betrivers", -115, -105), book("betus", -120, 100)]
    market = make_market(books, total=43.0, spread=-1.5)
    context = make_context(
        form={"GB": 3.0, "CHI": -48.0},
        usual={"GB": {"id": "00-LOVE", "name": "Jordan Love"}, "CHI": {"id": "00-CALE", "name": "Caleb Williams"}},
        qb={"00-LOVE": {"q": 22.0, "name": "Jordan Love", "team": "GB", "starts": 70, "last_start_date": "2026-10-11"},
            "00-CALE": {"q": 12.0, "name": "Caleb Williams", "team": "CHI", "starts": 30, "last_start_date": "2026-10-11"}},
    )
    return game, pregame, market, context


def scenario_starter_missing():
    """Away starter not listed (future week): QB term 0 for that side, no QB sentence."""
    game = make_game("2026_08_MIA_BUF", "BUF", "MIA", away_qb_id=None, away_qb_name=None,
                     home_qb_id="00-ALLE", home_qb_name="Josh Allen", home_rest=None, away_rest=None)
    pregame = make_pregame(1650.0, 1470.0, q_home=58.0, q_away=None)
    context = make_context(usual={"BUF": {"id": "00-ALLE", "name": "Josh Allen"}})
    return game, pregame, None, context


# ----------------------------------------------------------------------------
# Pure math
# ----------------------------------------------------------------------------


class BlendTests(unittest.TestCase):
    def test_spec_equal_weights(self):
        self.assertAlmostEqual(predict.blend(0.6, 0.5, 0.5), 0.55, places=12)

    def test_endpoints_and_missing_market(self):
        self.assertAlmostEqual(predict.blend(0.6, 0.5, 1.0), 0.6)
        self.assertAlmostEqual(predict.blend(0.6, 0.5, 0.0), 0.5)
        self.assertEqual(predict.blend(0.6, None, 0.35), 0.6)
        self.assertAlmostEqual(predict.blend(0.6, 0.5, 0.35), 0.35 * 0.6 + 0.65 * 0.5)

    def test_complementary(self):
        ph = predict.blend(0.6, 0.5, 0.35)
        pa = predict.blend(0.4, 0.5, 0.35)
        self.assertAlmostEqual(ph + pa, 1.0, places=12)


class ScoreTests(unittest.TestCase):
    def test_spec_gap_zero_neutral_total_44(self):
        eh, ea = predict.expected_scores(44.0, 0.0)
        self.assertEqual((eh, ea), (22.0, 22.0))
        self.assertAlmostEqual(eh - ea, 0.0)
        # D5 display tie rule: +1 to the favorite (home when p_home_model >= 0.5)
        self.assertEqual(predict.display_scores(eh, ea, True), (23, 22))
        self.assertEqual(predict.display_scores(eh, ea, False), (22, 23))

    def test_margin_equals_spread_and_sum_equals_total(self):
        for total, spread in ((44.0, 3.5), (51.0, -7.25), (38.5, 0.3)):
            eh, ea = predict.expected_scores(total, spread)
            self.assertAlmostEqual(eh - ea, spread, places=12)
            self.assertAlmostEqual(eh + ea, total, places=12)

    def test_display_rounds_half_up_and_stays_within_display_tolerance(self):
        self.assertEqual(predict.display_scores(26.5, 22.4, True), (27, 22))
        dh, da = predict.display_scores(20.49, 19.5, True)
        self.assertEqual((dh, da), (21, 20))
        self.assertLessEqual(abs(dh - 20.49), 1.0)
        self.assertLessEqual(abs(da - 19.5), 1.0)

    def test_display_equal_after_rounding_nudges_favorite_within_1_5(self):
        # D5: 22.85 / 22.65 both round to 23; the favorite gets +1 (up to 1.5 from its expected value)
        cases = ((22.85, 22.65, True, (24, 23)), (22.85, 22.65, False, (23, 24)),
                 (25.74, 25.76, False, (26, 27)), (20.89, 20.61, True, (22, 21)), (22.0, 22.0, True, (23, 22)))
        for eh, ea, fav_home, want in cases:
            dh, da = predict.display_scores(eh, ea, fav_home)
            self.assertEqual((dh, da), want, (eh, ea, fav_home))
            self.assertNotEqual(dh, da)
            self.assertLessEqual(abs(dh - eh), schema.DISPLAY_TOLERANCE)
            self.assertLessEqual(abs(da - ea), schema.DISPLAY_TOLERANCE)
            self.assertGreater(max(abs(dh - eh), abs(da - ea)), 0.5)  # the nudged side really exceeds the plain rounding bound

    def test_round_half_away(self):
        self.assertEqual(predict.round_half_away(2.5), 3)
        self.assertEqual(predict.round_half_away(-2.5), -3)
        self.assertEqual(predict.round_half_away(2.49), 2)
        self.assertEqual(predict.pct(0.745), 75)
        self.assertEqual(predict.pct(0.5336891951708406), 53)

    def test_ordinal(self):
        self.assertEqual([predict.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 32)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "32nd"])


class TotalSourceTests(unittest.TestCase):
    def test_priority(self):
        params = copy.deepcopy(PARAMS)
        params["score"]["league_average_total"] = 45.6
        game = make_game("2026_06_KC_DEN", "DEN", "KC", total_line=46.5)
        odds = make_market([book("draftkings", 200, -240)], total=47.0)
        self.assertEqual(predict.choose_total(odds, game, params), (47.0, "odds_api"))
        csv_mkt = dict(odds, source="csv", consensus_total=46.5)
        self.assertEqual(predict.choose_total(csv_mkt, game, params), (46.5, "csv"))
        odds_no_total = dict(odds, consensus_total=None)
        self.assertEqual(predict.choose_total(odds_no_total, game, params), (46.5, "csv"))
        self.assertEqual(predict.choose_total(None, game, params), (46.5, "csv"))
        game_no_line = make_game("2026_06_KC_DEN", "DEN", "KC")
        self.assertEqual(predict.choose_total(None, game_no_line, params), (45.6, "league_average"))


class PriceMathTests(unittest.TestCase):
    def test_decimal_profit(self):
        self.assertAlmostEqual(predict.decimal_profit(150), 1.5)
        self.assertAlmostEqual(predict.decimal_profit(-120), 100 / 120)
        self.assertAlmostEqual(predict.decimal_profit(100), 1.0)
        self.assertAlmostEqual(predict.decimal_profit(-100), 1.0)
        with self.assertRaises(ValueError):
            predict.decimal_profit(50)

    def test_ev(self):
        self.assertAlmostEqual(predict.estimated_ev(0.5, 100), 0.0)
        self.assertAlmostEqual(predict.estimated_ev(0.5, -110), 0.5 * 100 / 110 - 0.5)
        self.assertLess(predict.estimated_ev(0.5, -110), 0)
        self.assertAlmostEqual(predict.estimated_ev(0.5, -110), -0.04545454545, places=9)

    def test_matches_market_module_when_present(self):
        try:
            from src import market  # noqa: F401
        except ImportError:
            self.skipTest("src.market not built yet")
        for am in (100, -100, 150, -120, 400, -550):
            self.assertAlmostEqual(predict.decimal_profit(am), market.american_to_decimal_profit(am), places=12)
            self.assertAlmostEqual(predict.estimated_ev(0.61, am), market.estimated_ev(0.61, am), places=12)

    def test_best_of_prices_tie_goes_to_first_book(self):
        prices = {"draftkings": None, "fanduel": {"book": "fanduel", "side": "GB", "american": -115},
                  "betmgm": None, "betrivers": {"book": "betrivers", "side": "GB", "american": -115}}
        self.assertEqual(predict.best_of_prices(prices, list(PARAMS["books"]))["book"], "fanduel")
        prices["betrivers"]["american"] = -110
        self.assertEqual(predict.best_of_prices(prices, list(PARAMS["books"]))["book"], "betrivers")
        self.assertIsNone(predict.best_of_prices({b: None for b in PARAMS["books"]}, list(PARAMS["books"])))


# ----------------------------------------------------------------------------
# The official object
# ----------------------------------------------------------------------------


class OfficialObjectTests(unittest.TestCase):
    def test_all_scenarios_validate_against_schema(self):
        entries = []
        g, pg, mk, ctx = scenario_starter_change()
        entries.append(wrap(g, predict.predict_game(g, pg, mk, PARAMS, ctx)))
        g, pg, mk, ctx, params = scenario_neutral_no_market()
        entries.append(wrap(g, predict.predict_game(g, pg, mk, params, ctx)))
        g, pg, mk, ctx = scenario_csv_market()
        entries.append(wrap(g, predict.predict_game(g, pg, mk, PARAMS, ctx)))
        g, pg, mk, ctx = scenario_first_start_partial_books()
        entries.append(wrap(g, predict.predict_game(g, pg, mk, PARAMS, ctx)))
        g, pg, mk, ctx = scenario_starter_missing()
        entries.append(wrap(g, predict.predict_game(g, pg, mk, PARAMS, ctx)))
        g, pg, mk, ctx, params = scenario_injuries()
        entries.append(wrap(g, predict.predict_game(g, pg, mk, params, ctx)))
        self.assertEqual(validate_games(entries), [])
        for e in entries:
            self.assertEqual(set(e["official"]), schema.OFFICIAL_KEYS)
            self.assertEqual(set(e["official"]["factors"]), schema.FACTOR_KEYS)
            self.assertEqual(set(e["official"]["injuries"]), schema.INJURIES_KEYS)
        # the same scenarios with the injury term ON validate against params that enable it
        params = enabled_params()
        g, pg, mk, ctx, _ = scenario_injuries()
        entries = [wrap(g, predict.predict_game(g, pg, mk, params, ctx))]
        g, pg, mk, ctx = scenario_starter_change()
        entries.append(wrap(g, predict.predict_game(g, pg, mk, params, ctx)))
        self.assertEqual(validate_games(entries, params), [])

    def test_starter_change_scenario(self):
        g, pg, mk, ctx = scenario_starter_change()
        out = predict.predict_game(g, pg, mk, PARAMS, ctx)
        o, f = out["official"], out["official"]["factors"]
        self.assertEqual(o["pick"], "KC")
        self.assertLess(o["p_home_blend"], 0.5)
        self.assertAlmostEqual(o["p_pick_blend"], 1.0 - o["p_home_blend"], places=12)
        self.assertEqual(o["tier"], schema.tier_for(o["p_pick_blend"], PARAMS["tier_bounds"]))
        self.assertAlmostEqual(o["p_home_blend"], predict.blend(pg["p_home"], mk["consensus_p_home"], PARAMS["blend"]["model_weight"]))
        # factors
        self.assertEqual(f["elo_gap_home"], -140.0)
        self.assertEqual(f["hfa_elo"], 40.0)
        self.assertAlmostEqual(f["hfa_spread_pts"], 1.6)
        self.assertEqual(f["rest_diff"], 0)
        self.assertEqual(f["qb_elo"], -80.0)
        self.assertAlmostEqual(f["d_total"], -140 + 40 + 0 - 80)
        self.assertAlmostEqual(o["model_spread"], f["d_total"] / 25.0)
        self.assertTrue(f["qb_home"]["differs_from_usual"])
        self.assertEqual(f["qb_home"]["usual_starter_name"], "Bo Nix")
        self.assertEqual(f["qb_home"]["usual_starter_q"], 15.0)
        self.assertFalse(f["qb_away"]["differs_from_usual"])
        self.assertEqual(f["qb_away"]["usual_starter_q"], 60.0)
        self.assertAlmostEqual(f["model_market_gap_home"], pg["p_home"] - mk["consensus_p_home"])
        self.assertAlmostEqual(f["model_market_gap_pick"], -f["model_market_gap_home"])
        # scores from the odds_api total
        self.assertEqual(o["total_source"], "odds_api")
        self.assertEqual(o["total_used"], 46.5)
        self.assertAlmostEqual(o["expected_home"] + o["expected_away"], 46.5)
        self.assertAlmostEqual(o["expected_home"] - o["expected_away"], o["blend_spread"])
        # prices: pick side (away_ml) at exactly the default books; best = highest profit (fanduel -270)
        self.assertEqual(set(o["best_prices"]), set(PARAMS["books"]))
        self.assertEqual(o["best_prices"]["draftkings"], {"book": "draftkings", "side": "KC", "american": -150})
        self.assertEqual(o["best_price"], {"book": "fanduel", "side": "KC", "american": -148})
        self.assertAlmostEqual(o["estimated_ev_per_dollar"], predict.estimated_ev(o["p_pick_blend"], -148))
        self.assertTrue(o["parlay_eligible"])
        self.assertEqual(out["availability"], {"starter_home": True, "starter_away": True, "market": True, "csv_lines": True})
        # edge: gap on the pick side is large, and a starter change is measured
        self.assertGreaterEqual(abs(f["model_market_gap_pick"]), schema.EDGE_THRESHOLD)
        self.assertEqual(o["edge"]["reason_key"], "starter_change")
        self.assertIn("Denver", o["edge"]["reason_text"])

    def test_neutral_no_market_scenario(self):
        g, pg, mk, ctx, params = scenario_neutral_no_market()
        out = predict.predict_game(g, pg, mk, params, ctx)
        o, f = out["official"], out["official"]["factors"]
        self.assertIsNone(o["p_home_market"])
        self.assertEqual(o["p_home_blend"], o["p_home_model"])
        self.assertAlmostEqual(o["p_home_model"], 0.5)
        self.assertEqual(o["pick"], "JAX")  # p_home_blend >= 0.5 -> home
        self.assertEqual(o["tier"], "Toss-up")
        self.assertEqual(f["hfa_elo"], 0.0)
        self.assertEqual(o["model_spread"], 0.0)
        self.assertEqual((o["expected_home"], o["expected_away"]), (22.0, 22.0))
        self.assertEqual((o["display_home"], o["display_away"]), (23, 22))
        self.assertEqual(o["total_source"], "league_average")
        self.assertEqual(o["total_used"], 44.0)
        self.assertIsNone(o["market"])
        self.assertEqual(o["best_prices"], {b: None for b in params["books"]})
        self.assertIsNone(o["best_price"])
        self.assertIsNone(o["estimated_ev_per_dollar"])
        self.assertIsNone(o["edge"])
        self.assertFalse(o["parlay_eligible"])
        self.assertIsNone(f["model_market_gap_home"])
        self.assertEqual(o["rationale"], ["rating", "verdict"])
        self.assertIn("rate even", o["rationale_sentences"][0])
        self.assertTrue(o["rationale_sentences"][-1].startswith("No market line yet"))
        self.assertFalse(out["availability"]["market"])
        self.assertFalse(out["availability"]["csv_lines"])

    def test_csv_market_scenario(self):
        g, pg, mk, ctx = scenario_csv_market()
        o = predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]
        self.assertEqual(o["pick"], "NYJ")
        self.assertEqual(o["best_prices"], {b: None for b in PARAMS["books"]})
        self.assertEqual(o["best_price"], {"book": "csv", "side": "NYJ", "american": -150})
        self.assertAlmostEqual(o["estimated_ev_per_dollar"], predict.estimated_ev(o["p_pick_blend"], -150))
        self.assertEqual(o["total_source"], "csv")
        self.assertEqual(o["total_used"], 44.5)
        self.assertEqual(o["factors"]["rest_diff"], 4)
        self.assertEqual(o["factors"]["rest_elo"], 8.0)
        self.assertIn("rest", o["rationale"])
        self.assertEqual(o["parlay_eligible"], o["p_pick_blend"] >= 0.55)

    def test_first_start_and_partial_books(self):
        g, pg, mk, ctx = scenario_first_start_partial_books()
        o, f = (lambda out: (out["official"], out["official"]["factors"]))(predict.predict_game(g, pg, mk, PARAMS, ctx))
        self.assertEqual(o["pick"], "GB")
        self.assertTrue(f["qb_home"]["first_start"])
        self.assertEqual(f["qb_home"]["starts"], 0)
        self.assertTrue(f["qb_home"]["differs_from_usual"])
        self.assertIsNone(o["best_prices"]["draftkings"])
        self.assertIsNone(o["best_prices"]["betmgm"])
        self.assertEqual(o["best_prices"]["fanduel"]["american"], -115)
        self.assertEqual(o["best_price"]["book"], "fanduel")  # tie with betrivers -> first in params.books
        self.assertIn("quarterback", o["rationale"])
        qb_text = o["rationale_sentences"][o["rationale"].index("quarterback")]
        self.assertIn("first start on record", qb_text)
        self.assertIn("listed starter", qb_text)
        self.assertNotIn("below their usual starter", qb_text)
        self.assertIn("form", o["rationale"])
        self.assertIn("Chicago has lost 48 points", o["rationale_sentences"][o["rationale"].index("form")])
        self.assertEqual(o["edge"]["reason_key"], "starter_change")

    def test_starter_missing(self):
        g, pg, mk, ctx = scenario_starter_missing()
        out = predict.predict_game(g, pg, mk, PARAMS, ctx)
        o, f = out["official"], out["official"]["factors"]
        self.assertFalse(out["availability"]["starter_away"])
        self.assertTrue(out["availability"]["starter_home"])
        self.assertIsNone(f["qb_away"])
        self.assertEqual(f["qb_elo"], 58.0)
        self.assertNotIn("quarterback", o["rationale"])
        self.assertIsNone(f["rest_diff"])
        self.assertEqual(f["rest_elo"], 0.0)
        self.assertIsNone(f["rest_home"])
        self.assertNotIn("rest", o["rationale"])
        self.assertEqual(o["rationale"], ["rating", "home_field", "verdict"])

    def test_pick_follows_blend_not_model(self):
        # model favors home (0.55) but the market strongly favors away -> blended pick is away
        g = make_game("2026_06_SF_SEA", "SEA", "SF")
        pg = make_pregame(1540.0, 1500.0, q_home=0.0, q_away=5.0)
        mk = make_market([book("draftkings", 180, -220), book("fanduel", 185, -225)])
        o = predict.predict_game(g, pg, mk, PARAMS, make_context())["official"]
        self.assertGreater(o["p_home_model"], 0.5)
        self.assertLess(o["p_home_blend"], 0.5)
        self.assertEqual(o["pick"], "SF")
        self.assertEqual(o["best_price"]["side"], "SF")
        self.assertEqual(o["best_price"]["american"], -220)
        # D5 (amended): the score follows the blend, so the pick is always the higher score ...
        self.assertGreater(o["display_away"], o["display_home"])
        self.assertLess(o["blend_spread"], 0.0)
        self.assertGreater(o["model_spread"], 0.0)
        self.assertIn("market", o["rationale"])
        # ... and the verdict calls it predicted
        verdict = o["rationale_sentences"][-1]
        self.assertIn("San Francisco at", verdict)
        self.assertIn("predicted %d to %d." % (o["display_away"], o["display_home"]), verdict)

    def test_usual_starter_q_is_inactivity_decayed_like_the_listed_starter(self):
        # usual starter idle for 2+ years: the comparison uses the decayed Q (-25 per completed year)
        g = make_game("2026_06_KC_DEN", "DEN", "KC", league_date="2026-10-18",
                      home_qb_id="00-STID", home_qb_name="Jarrett Stidham", away_qb_id="00-MAHO", away_qb_name="Patrick Mahomes")
        pg = make_pregame(1520.0, 1660.0, q_home=-20.0, q_away=60.0, starts_home=5, starts_away=120)
        ctx = make_context(
            usual={"DEN": {"id": "00-NIX", "name": "Bo Nix"}, "KC": {"id": "00-MAHO", "name": "Patrick Mahomes"}},
            qb={"00-NIX": {"q": 15.0, "name": "Bo Nix", "team": "DEN", "starts": 40, "last_start_date": "2024-09-01"},
                "00-STID": {"q": -20.0, "name": "Jarrett Stidham", "team": "DEN", "starts": 5, "last_start_date": "2026-10-11"},
                "00-MAHO": {"q": 60.0, "name": "Patrick Mahomes", "team": "KC", "starts": 120, "last_start_date": "2026-10-11"}})
        f = predict.predict_game(g, pg, None, PARAMS, ctx)["official"]["factors"]
        penalty = float(PARAMS["qb"]["inactivity_penalty"])
        self.assertAlmostEqual(f["qb_home"]["usual_starter_q"], 15.0 - 2 * penalty)
        self.assertAlmostEqual(f["qb_away"]["usual_starter_q"], 60.0)  # the listed starter IS the usual one: its own q


# ----------------------------------------------------------------------------
# Rationale rules and number provenance
# ----------------------------------------------------------------------------


def numeric_leaves(obj):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        yield float(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from numeric_leaves(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from numeric_leaves(v)


def allowed_numbers(official):
    """Every number a sentence may contain: factor/official values and their
    roundings, absolute values, percents, complements and the QB-vs-usual gap."""
    f = official["factors"]
    base = list(numeric_leaves(f))
    for k in ("p_home_model", "p_home_market", "p_home_blend", "p_pick_blend", "model_spread", "blend_spread",
              "expected_home", "expected_away", "display_home", "display_away", "total_used"):
        if official[k] is not None:
            base.append(float(official[k]))
    for side in ("qb_home", "qb_away"):
        qb = f[side]
        if qb and qb["usual_starter_q"] is not None:
            base.append(qb["q"] - qb["usual_starter_q"])
    inj = official.get("injuries") or {}
    base.extend(numeric_leaves({k: v for k, v in inj.items() if k != "as_of"}))
    for side in ("home", "away"):
        for k in ("starters_out", "starters_doubtful"):  # "... and N more" after the 3 named positions
            base.append(float(max(0, int((inj.get(side) or {}).get(k) or 0) - predict.INJURY_NAMED_POSITIONS)))
    allowed = set()
    for v in base:
        for x in (v, abs(v)):
            allowed.add(x)
            allowed.add(float(predict.round_half_away(x)))
            allowed.add(round(x, 1))
            if 0.0 <= x <= 1.0:
                allowed.add(float(predict.pct(x)))
                allowed.add(float(predict.pct(1.0 - x)))
    return allowed


class RationaleTests(unittest.TestCase):
    def outputs(self):
        outs = []
        g, pg, mk, ctx = scenario_starter_change()
        outs.append((g, predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]))
        g, pg, mk, ctx, params = scenario_neutral_no_market()
        outs.append((g, predict.predict_game(g, pg, mk, params, ctx)["official"]))
        g, pg, mk, ctx = scenario_csv_market()
        outs.append((g, predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]))
        g, pg, mk, ctx = scenario_first_start_partial_books()
        outs.append((g, predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]))
        g, pg, mk, ctx = scenario_starter_missing()
        outs.append((g, predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]))
        g, pg, mk, ctx, params = scenario_injuries()
        outs.append((g, predict.predict_game(g, pg, mk, params, ctx)["official"]))
        outs.append((g, predict.predict_game(g, pg, mk, enabled_params(), ctx)["official"]))
        return outs

    def test_every_number_in_every_sentence_is_in_factors_or_official(self):
        for g, o in self.outputs():
            allowed = allowed_numbers(o)
            for key, sentence in zip(o["rationale"], o["rationale_sentences"]):
                for tok in NUM.findall(sentence):
                    val = float(tok)
                    ok = any(abs(val - a) < 1e-9 for a in allowed)
                    self.assertTrue(ok, "%s/%s: number %s in %r is not a factor value" % (g["game_id"], key, tok, sentence))

    def test_structure_and_wording_rules(self):
        for g, o in self.outputs():
            keys, sents = o["rationale"], o["rationale_sentences"]
            self.assertEqual(len(keys), len(sents))
            self.assertEqual(keys[0], "rating")
            self.assertEqual(keys[-1], "verdict")
            self.assertLessEqual(len(sents), 7)
            self.assertGreaterEqual(len(sents), 2)
            self.assertEqual(keys, schema.expected_rationale_keys(wrap(g, {"official": o, "availability": {}})))
            self.assertTrue(all(k in schema.RATIONALE_KEYS for k in keys))
            self.assertEqual(len(set(keys)), len(keys))
            text = " ".join(sents)
            # D17/D18: injuries are mentioned ONLY by the injuries sentence, and only as listed statuses
            for key, sent in zip(keys, sents):
                if key != "injuries":
                    self.assertNotIn("injur", sent.lower(), sent)
                else:
                    self.assertIn("lists", sent)
            self.assertNotIn("injury news", text.lower())
            self.assertNotIn("edge", text.lower())
            if "quarterback" in keys:
                self.assertIn("listed starter", sents[keys.index("quarterback")])
            if "market" in keys:
                self.assertIn("may reflect information this model lacks", sents[keys.index("market")])
            if g["neutral"]:
                self.assertNotIn("home_field", keys)
            pick_label = predict.team_label(o["pick"], g["home"] if o["pick"] == g["away"] else g["away"], {t["code"]: t for t in schema.TEAM_META})
            self.assertIn(pick_label, sents[-1])
            self.assertIn(o["tier"], sents[-1])
            x, y = (o["display_home"], o["display_away"]) if o["pick"] == g["home"] else (o["display_away"], o["display_home"])
            self.assertIn("predicted %d to %d" % (x, y) if x > y else "the model alone scores it", sents[-1])

    def test_absent_factors_are_not_mentioned(self):
        g, pg, mk, ctx = scenario_starter_change()  # rest equal -> no rest sentence
        o = predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]
        self.assertNotIn("rest", o["rationale"])
        self.assertNotIn("days of rest", " ".join(o["rationale_sentences"]))

    def test_every_triggered_factor_is_kept_in_contract_order(self):
        g, pg, mk, ctx = scenario_starter_change()
        o = predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]
        # CONTRACT 2.6: fixed order, no budget: home field (a factor the model used) is never dropped
        self.assertEqual(o["rationale"], ["rating", "home_field", "quarterback", "form", "market", "verdict"])
        self.assertEqual(o["rationale_sentences"][0], "Kansas City rates 140 Elo points above Denver, 1st in the league against 16th.")
        self.assertEqual(o["rationale_sentences"][1], "Home field is worth about 40 points, or 1.6 points on the spread.")
        self.assertEqual(o["rationale_sentences"][2], "Denver's listed starter, Jarrett Stidham, rates 35 points below their usual starter, Bo Nix.")
        self.assertEqual(o["rationale_sentences"][3], "Kansas City has gained 46 points of rating over its last three games.")
        self.assertTrue(o["rationale_sentences"][4].startswith("The books have Kansas City at "))
        # all eight keys at once: add a rest gap and a listed starter to the same scenario
        g = dict(g, home_rest=10, away_rest=6)
        pg = dict(pg, rest_elo=8.0)
        ctx = with_injuries(ctx, g, make_report(player("Lineman", "T", "Out")), make_report())
        o = predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]
        self.assertEqual(o["rationale"], list(schema.RATIONALE_KEYS))
        self.assertEqual(len(o["rationale_sentences"]), 8)
        self.assertEqual(o["rationale_sentences"][4],
                         "Denver lists 1 starter out (T); Kansas City lists none; shown for context, not in the number yet.")

    def test_expected_rationale_keys_matches_triggers(self):
        g, pg, mk, ctx = scenario_csv_market()  # rest gap 4, nothing else
        entry = wrap(g, predict.predict_game(g, pg, mk, PARAMS, ctx))
        self.assertEqual(schema.expected_rationale_keys(entry), ["rating", "home_field", "rest", "verdict"])
        self.assertEqual(entry["official"]["rationale"], ["rating", "home_field", "rest", "verdict"])
        bad = copy.deepcopy(entry)
        bad["official"]["rationale"] = ["rating", "rest", "home_field", "verdict"]
        bad["official"]["rationale_sentences"] = [bad["official"]["rationale_sentences"][i] for i in (0, 2, 1, 3)]
        self.assertTrue(any("order" in p for p in validate_games([bad])))
        bad["official"]["rationale"] = ["rating", "home_field", "verdict"]
        bad["official"]["rationale_sentences"] = bad["official"]["rationale_sentences"][:2] + bad["official"]["rationale_sentences"][-1:]
        self.assertTrue(any("triggers" in p for p in validate_games([bad])))

    def test_market_sentence_direction_words(self):
        g, pg, mk, ctx = scenario_starter_change()
        o = predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]
        s = o["rationale_sentences"][o["rationale"].index("market")]
        p_model = 1 - o["p_home_model"]
        p_market = 1 - o["p_home_market"]
        self.assertIn("closer than" if p_market < p_model else "further than", s)
        self.assertIn("%d%%" % predict.pct(p_market), s)
        self.assertIn("%d%%" % predict.pct(p_model), s)

    def test_shared_city_uses_full_names(self):
        names = {t["code"]: t for t in schema.TEAM_META}
        self.assertEqual(predict.team_label("NYJ", "NYG", names), "New York Jets")
        self.assertEqual(predict.team_label("NYG", "NYJ", names), "New York Giants")
        self.assertEqual(predict.team_label("LA", "LAC", names), "Los Angeles Rams")
        self.assertEqual(predict.team_label("KC", "DEN", names), "Kansas City")
        self.assertEqual(predict.team_label("NYJ", "CLE", names), "New York")

    def test_quarterback_gap_sentence_without_change(self):
        g = make_game("2026_06_CIN_MIA", "MIA", "CIN", home_qb_id="00-WILL", home_qb_name="Malik Willis",
                      away_qb_id="00-BURR", away_qb_name="Joe Burrow")
        pg = make_pregame(1466.0, 1503.0, q_home=-27.6, q_away=44.6)
        ctx = make_context(usual={"MIA": {"id": "00-WILL", "name": "Malik Willis"}, "CIN": {"id": "00-BURR", "name": "Joe Burrow"}})
        o = predict.predict_game(g, pg, None, PARAMS, ctx)["official"]
        s = o["rationale_sentences"][o["rationale"].index("quarterback")]
        self.assertEqual(s, "Cincinnati's listed starter, Joe Burrow, rates 72 points above the other listed starter.")


class LiveModelTests(unittest.TestCase):
    """Checks over the repo's real model.json when present (skipped otherwise)."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(FIX), "..", "model.json")
        cls.model = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                cls.model = json.load(fh)

    def test_verdict_matches_the_displayed_score_for_every_game(self):
        if self.model is None:
            self.skipTest("model.json not built")
        if "injury" not in (self.model.get("params") or {}):
            self.skipTest("model.json predates D18 (no params.injury); rebuild it")
        self.assertEqual(schema.validate_model(self.model)[:5], [])
        disagree = 0
        for g in self.model["games"]:
            o = g["official"]
            x, y = (o["display_home"], o["display_away"]) if o["pick"] == g["home"] else (o["display_away"], o["display_home"])
            verdict = o["rationale_sentences"][-1]
            self.assertNotEqual(x, y, g["game_id"])
            if x > y:
                self.assertIn("predicted %d to %d." % (x, y), verdict, g["game_id"])
            else:
                disagree += 1
                self.assertIn("the model alone scores it", verdict, g["game_id"])
                self.assertIn("%d to %d." % (y, x), verdict, g["game_id"])
                self.assertIsNotNone(o["p_home_market"], g["game_id"])  # only a market can overrule the model
            self.assertEqual(o["rationale"], schema.expected_rationale_keys(g), g["game_id"])
        self.assertLess(disagree, len(self.model["games"]) // 4)  # sanity bound; today 14 of 272


# ----------------------------------------------------------------------------
# Edge chip priority (D17)
# ----------------------------------------------------------------------------


def factors_for_edge(gap=0.1, differs_home=False, rest_diff=0, form_home=0.0, form_away=0.0):
    def qb(differs):
        return {"id": "x", "name": "QB", "q": 0.0, "starts": 5, "first_start": False, "usual_starter_id": "u",
                "usual_starter_name": "Usual", "usual_starter_q": 0.0, "differs_from_usual": differs, "source": "nflverse_listed"}
    return {
        "team_elo_home": 1500.0, "team_elo_away": 1500.0, "elo_gap_home": 0.0, "home_rank": 1, "away_rank": 2,
        "hfa_elo": 40.0, "hfa_spread_pts": 1.6, "rest_home": 7 + rest_diff, "rest_away": 7, "rest_diff": rest_diff,
        "rest_elo": 2.0 * rest_diff, "qb_home": qb(differs_home), "qb_away": qb(False), "qb_elo": 0.0,
        "form_home_3": form_home, "form_away_3": form_away, "d_total": 40.0,
        "model_market_gap_home": gap, "model_market_gap_pick": gap,
    }


class EdgeChipTests(unittest.TestCase):
    names = {"home": "Denver", "away": "Kansas City"}

    def test_below_threshold_or_no_market_is_null(self):
        self.assertIsNone(predict.edge_chip(factors_for_edge(gap=0.079), self.names))
        self.assertIsNone(predict.edge_chip(factors_for_edge(gap=-0.05), self.names))
        self.assertIsNone(predict.edge_chip(factors_for_edge(gap=None), self.names))

    def test_priority_and_texts(self):
        e = predict.edge_chip(factors_for_edge(differs_home=True, rest_diff=5, form_away=-60.0), self.names)
        self.assertEqual(e, {"flag": True, "reason_key": "starter_change", "reason_text": "Denver's listed starter differs from the usual starter"})
        e = predict.edge_chip(factors_for_edge(rest_diff=-3, form_away=-60.0), self.names)
        self.assertEqual(e, {"flag": True, "reason_key": "rest_gap", "reason_text": "Kansas City has 3 more days of rest"})
        e = predict.edge_chip(factors_for_edge(gap=-0.08, form_away=-55.3), self.names)
        self.assertEqual(e, {"flag": True, "reason_key": "form_move", "reason_text": "Kansas City's rating moved 55 points in the last three games"})
        e = predict.edge_chip(factors_for_edge(rest_diff=2, form_home=39.9), self.names)
        self.assertEqual(e, {"flag": True, "reason_key": "unknown", "reason_text": "may reflect information this model lacks"})
        self.assertNotIn("injur", json.dumps(e))

    def test_injuries_reason_sits_between_starter_change_and_rest_gap(self):
        inj = injuries.injuries_block(DEN_REPORT, LAC_FILED_NONE, PARAMS, AS_OF)
        allowed = schema.EDGE_REASON_KEYS + ("injuries",)
        with mock.patch.object(schema, "EDGE_REASON_KEYS", allowed):
            self.assertTrue(predict.edge_reason_allowed("injuries"))
            # a starter change still wins
            e = predict.edge_chip(factors_for_edge(differs_home=True, rest_diff=5), self.names, inj)
            self.assertEqual(e["reason_key"], "starter_change")
            # then injuries, ahead of a rest gap and a form move
            e = predict.edge_chip(factors_for_edge(rest_diff=5, form_away=-60.0), self.names, inj)
            self.assertEqual(e, {"flag": True, "reason_key": "injuries",
                                 "reason_text": "Denver lists 4 starters Out (S, WR, LT and 1 more) and 1 Doubtful (RB)"})
            # the side with more listed starters is named; Doubtful alone reads as such
            inj2 = injuries.injuries_block(make_report(), make_report(player("Edge", "DE", "Doubtful")), PARAMS, AS_OF)
            e = predict.edge_chip(factors_for_edge(rest_diff=5), self.names, inj2)
            self.assertEqual(e["reason_text"], "Kansas City lists 1 starter Doubtful (DE)")
            # Questionable alone, an unfiled or an unavailable report never earn the chip
            for quiet in (injuries.injuries_block(make_report(player("CB", "CB", "Questionable")), make_report(filed=False), PARAMS, AS_OF),
                          injuries.injuries_block(injuries.unavailable_report(), injuries.unavailable_report(), PARAMS, AS_OF)):
                e = predict.edge_chip(factors_for_edge(rest_diff=5), self.names, quiet)
                self.assertEqual(e["reason_key"], "rest_gap")
            self.assertIsNone(predict.edge_chip(factors_for_edge(gap=0.05), self.names, inj))
        # while the schema does not admit the key (the contract's EDGE_REASON_KEYS today) the chip
        # falls through to the next measured factor so validate_model never sees an unknown key
        if "injuries" not in schema.EDGE_REASON_KEYS:
            e = predict.edge_chip(factors_for_edge(rest_diff=5), self.names, inj)
            self.assertEqual(e["reason_key"], "rest_gap")
            e = predict.edge_chip(factors_for_edge(), self.names, inj)
            self.assertEqual(e["reason_key"], "unknown")
        for key in ("starter_change", "rest_gap", "form_move", "unknown"):
            self.assertTrue(predict.edge_reason_allowed(key))


# ----------------------------------------------------------------------------
# Injury reports (D18)
# ----------------------------------------------------------------------------


class InjuryTests(unittest.TestCase):
    def test_no_context_entry_means_unavailable_reports_and_zero_term(self):
        g, pg, mk, ctx = scenario_starter_change()
        o = predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]
        inj = o["injuries"]
        self.assertEqual(set(inj), schema.INJURIES_KEYS)
        self.assertEqual(inj["home"], injuries.unavailable_report())
        self.assertEqual(inj["away"], injuries.unavailable_report())
        self.assertEqual((inj["enabled"], inj["elo_home"], inj["elo_away"], inj["elo"]), (False, 0.0, 0.0, 0.0))
        self.assertEqual(inj["as_of"], g["kickoff_utc"])  # fallback: a valid, stable ISO time
        self.assertNotIn("injuries", o["rationale"])
        self.assertEqual(o["p_home_model"], pg["p_home"])
        # the contract's as_of travels through the context entry
        ctx2 = with_injuries(ctx, g, injuries.unavailable_report(), injuries.unavailable_report(), as_of="2026-10-17T12:00:00Z")
        self.assertEqual(predict.predict_game(g, pg, mk, PARAMS, ctx2)["official"]["injuries"]["as_of"], "2026-10-17T12:00:00Z")

    def test_block_carries_the_reports_and_disabled_term_is_zero(self):
        g, pg, mk, ctx, params = scenario_injuries()
        o = predict.predict_game(g, pg, mk, params, ctx)["official"]
        inj = o["injuries"]
        self.assertIs(inj["enabled"], False)
        self.assertEqual(inj["as_of"], AS_OF)
        self.assertEqual(inj["home"], DEN_REPORT)
        self.assertEqual(inj["away"], LAC_FILED_NONE)
        self.assertEqual((inj["home"]["starters_out"], inj["home"]["starters_doubtful"], inj["home"]["starters_questionable"]), (4, 1, 1))
        self.assertEqual((inj["elo_home"], inj["elo_away"], inj["elo"]), (0.0, 0.0, 0.0))
        # disabled: the replay's probability and d are untouched, bit for bit
        self.assertEqual(o["p_home_model"], pg["p_home"])
        self.assertEqual(o["factors"]["d_total"], pg["d"])
        self.assertEqual(o["model_spread"], pg["d"] / 25.0)

    def test_injuries_sentence_iff_a_starter_is_out_or_doubtful(self):
        g, pg, mk, ctx, params = scenario_injuries()
        cases = [
            (make_report(player("A", "WR", "Out")), make_report(), True),
            (make_report(), make_report(player("B", "CB", "Doubtful")), True),
            (make_report(player("A", "WR", "Questionable")), make_report(player("B", "CB", "Questionable")), False),
            (make_report(player("A", "WR", "Out", starter=False)), make_report(), False),  # depth player
            (make_report(player("Bo Nix", "QB", "Out")), make_report(), False),  # the QB term owns the quarterback
            (make_report(filed=False), make_report(filed=False), False),
            (injuries.unavailable_report(), injuries.unavailable_report(), False),
        ]
        for home_rep, away_rep, expect in cases:
            c = with_injuries(ctx, g, home_rep, away_rep)
            out = predict.predict_game(g, pg, mk, params, c)
            entry = wrap(g, out)
            keys = out["official"]["rationale"]
            self.assertEqual("injuries" in keys, expect, (home_rep, away_rep))
            self.assertEqual(keys, schema.expected_rationale_keys(entry))
            self.assertEqual(validate_games([entry]), [])
            self.assertEqual("injuries" in keys, schema.injury_rationale_triggers(out["official"]["injuries"]))

    def sentence(self, home_rep, away_rep, params=None):
        g, pg, mk, ctx, params = scenario_injuries(params)
        o = predict.predict_game(g, pg, mk, params, with_injuries(ctx, g, home_rep, away_rep))["official"]
        return o, o["rationale_sentences"][o["rationale"].index("injuries")]

    def test_sentence_wording_positions_and_the_quiet_side(self):
        o, s = self.sentence(DEN_REPORT, LAC_FILED_NONE)
        self.assertEqual(s, "Denver lists 4 starters out (S, WR, LT and 1 more) and 1 doubtful (RB); "  # 2.6b order: Out, then name
                            "Los Angeles lists none; shown for context, not in the number yet.")
        # the quiet side only "lists none" when its report is filed
        _, s = self.sentence(make_report(player("A", "WR", "Out")), make_report(filed=False))
        self.assertEqual(s, "Denver lists 1 starter out (WR); Los Angeles has not filed a report yet; shown for context, not in the number yet.")
        _, s = self.sentence(make_report(player("A", "WR", "Out")), injuries.unavailable_report())
        self.assertEqual(s, "Denver lists 1 starter out (WR); Los Angeles's report is unavailable; shown for context, not in the number yet.")
        # the side with more listed starters goes first; doubtful alone reads as "starter doubtful"
        _, s = self.sentence(make_report(player("A", "WR", "Doubtful")), make_report(player("B", "T", "Out"), player("C", "G", "Out")))
        self.assertEqual(s, "Los Angeles lists 2 starters out (T, G); Denver lists 1 starter doubtful (WR); shown for context, not in the number yet.")
        # exactly three positions are named in full; a quarterback on the report is never counted
        _, s = self.sentence(make_report(player("A", "WR", "Out"), player("B", "TE", "Out"), player("C", "C", "Out"), player("Bo Nix", "QB", "Out")),
                             make_report())
        self.assertEqual(s, "Denver lists 3 starters out (WR, TE, C); Los Angeles lists none; shown for context, not in the number yet.")
        self.assertNotIn("QB", s)
        self.assertNotIn("Nix", s)
        # a position missing from the report falls back to the group
        _, s = self.sentence(make_report(player("A", None, "Out", group="DB")), make_report())
        self.assertIn("(DB)", s)

    def test_enabled_term_enters_the_probability_and_the_sentence(self):
        params = enabled_params()
        g, pg, mk, ctx, _ = scenario_injuries()
        o = predict.predict_game(g, pg, mk, params, ctx)["official"]
        inj = o["injuries"]
        self.assertIs(inj["enabled"], True)
        # D18.6: WR 7 + LT 6 + CB 5 + S 5 Out, RB 4 * 0.75 Doubtful, Questionable DE 6 * 0.25; depth and QB add nothing
        self.assertAlmostEqual(inj["elo_home"], -(7 + 6 + 5 + 5 + 3 + 1.5))
        self.assertAlmostEqual(inj["elo_away"], -1.5)  # the Chargers' Questionable DE: 6 * 0.25; the depth LB adds nothing
        self.assertAlmostEqual(inj["elo"], inj["elo_home"] - inj["elo_away"])
        self.assertAlmostEqual(o["factors"]["d_total"], pg["d"] + inj["elo"])
        self.assertAlmostEqual(o["p_home_model"], elo.win_prob(o["factors"]["d_total"]))
        self.assertLess(o["p_home_model"], pg["p_home"])
        self.assertAlmostEqual(o["model_spread"], o["factors"]["d_total"] / 25.0)
        s = o["rationale_sentences"][o["rationale"].index("injuries")]
        # in the number: the Questionable starters are listed too (they move it), the value is given in
        # Elo points AND on the spread (26 / 25 = 1.04 -> "1 point"), never bare "points"
        self.assertEqual(s, "Denver lists 4 starters out (S, WR, LT and 1 more), 1 doubtful (RB) and 1 questionable (DE); "
                            "Los Angeles lists 1 starter questionable (DE); worth about 26 Elo points to Los Angeles, or 1 point on the spread.")
        self.assertEqual(inj["elo"], -26.0)
        self.assertNotIn("shown for context", s)
        entry = wrap(g, {"official": o, "availability": {"starter_home": True, "starter_away": True, "market": True, "csv_lines": True}})
        self.assertEqual(validate_games([entry], params), [])
        # a net of one point is singular
        c = with_injuries(ctx, g, make_report(player("A", "T", "Out")), make_report(player("B", "WR", "Out")))
        o1 = predict.predict_game(g, pg, mk, params, c)["official"]
        self.assertEqual(o1["injuries"]["elo"], 1.0)  # home loses an OL (6), away a WR (7): +1 to Denver
        self.assertTrue(o1["rationale_sentences"][o1["rationale"].index("injuries")].endswith("; worth about 1 Elo point to Denver, or 0.04 points on the spread."))
        # the two sides cancelling out is said as such, never "worth about 0 points"
        c = with_injuries(ctx, g, make_report(player("A", "WR", "Out")), make_report(player("B", "TE", "Out")))
        o2 = predict.predict_game(g, pg, mk, params, c)["official"]
        self.assertEqual(o2["injuries"]["elo"], 0.0)
        self.assertTrue(o2["rationale_sentences"][o2["rationale"].index("injuries")].endswith("; the two reports cancel out in the number."))
        self.assertEqual(o2["p_home_model"], pg["p_home"])

    def test_replay_carried_term_is_not_counted_twice(self):
        params = enabled_params()
        g, pg, mk, ctx, _ = scenario_injuries()
        fresh = predict.predict_game(g, pg, mk, params, ctx)["official"]
        term = fresh["injuries"]["elo"]
        # a played game: the replay already folded the same term into d / p_home and says so
        carried = dict(pg, d=pg["d"] + term, p_home=elo.win_prob(pg["d"] + term), injury_elo=term)
        o = predict.predict_game(g, carried, mk, params, ctx)["official"]
        self.assertAlmostEqual(o["p_home_model"], fresh["p_home_model"])
        self.assertAlmostEqual(o["factors"]["d_total"], fresh["factors"]["d_total"])
        self.assertAlmostEqual(o["p_home_model"], carried["p_home"])
        # the report-derived term wins over a stale carried one, and the term is removed when disabled
        stale = dict(carried, injury_elo=-5.0, d=pg["d"] - 5.0, p_home=elo.win_prob(pg["d"] - 5.0))
        o = predict.predict_game(g, stale, mk, params, ctx)["official"]
        self.assertAlmostEqual(o["p_home_model"], fresh["p_home_model"])
        o = predict.predict_game(g, carried, mk, PARAMS, ctx)["official"]
        self.assertAlmostEqual(o["p_home_model"], pg["p_home"])
        # model_probability without a d recomputes it from the components
        no_d = {k: v for k, v in carried.items() if k != "d"}
        self.assertAlmostEqual(predict.model_probability(no_d, term, params), fresh["p_home_model"])

    def test_frozen_official_keeps_the_rebuilt_report_and_the_snapshot_numbers(self):
        params = enabled_params()
        g, pg, mk, ctx, _ = scenario_injuries()
        live = predict.predict_game(g, pg, mk, params, ctx)["official"]
        snap = {"id": g["game_id"] + "@20261017T140000Z-abc1234", "game_id": g["game_id"], "kickoff_utc": g["kickoff_utc"],
                "build_at": "2026-10-17T14:00:00Z", "pick": "LAC", "p_model": 0.47, "p_market": 0.57, "p_blend": 0.535,
                "tier": "Toss-up", "pred_home": 21, "pred_away": 23, "best_price": -125, "best_book": "fanduel", "ev": -0.03,
                "market_source": "odds_api", "total_used": 44.0, "total_source": "odds_api",
                "injury_elo": -20.0, "inj_home": {"available": True, "report_filed": True, "starters_out": 3,
                                                  "starters_doubtful": 0, "starters_questionable": 0, "qb_listed": None},
                "inj_away": {"available": True, "report_filed": False, "starters_out": 0, "starters_doubtful": 0,
                             "starters_questionable": 0, "qb_listed": None}}
        o = predict.freeze_official(live, snap, g, pg, params, ctx)
        # 2.6a: the numbers are the snapshot's ...
        self.assertEqual((o["pick"], o["p_pick_blend"], o["tier"], o["display_home"], o["display_away"]), ("LAC", 0.535, "Toss-up", 21, 23))
        self.assertAlmostEqual(o["p_home_model"], 1 - 0.47)
        self.assertEqual(o["best_price"], {"book": "fanduel", "side": "LAC", "american": -125})
        self.assertIs(o["parlay_eligible"], False)
        # ... while the injuries block is the one rebuilt from the season files (2.6b), not the snapshot counts
        self.assertEqual(o["injuries"], live["injuries"])
        self.assertEqual(o["injuries"]["home"]["starters_out"], 4)
        # ... and d_total carries the SNAPSHOT's term (-20), not the latest report's (-26): the frozen
        # probability was made with the former, and the sentence says both (CONTRACT 2.6a)
        self.assertAlmostEqual(o["factors"]["d_total"], pg["d"] + snap["injury_elo"])
        self.assertAlmostEqual(o["model_spread"], o["factors"]["d_total"] / 25.0)
        self.assertIn("injuries", o["rationale"])
        s = o["rationale_sentences"][o["rationale"].index("injuries")]
        self.assertTrue(s.endswith("; the frozen pick carried about 20 Elo points to Los Angeles, or 0.8 points on the spread; "
                                   "the latest report reads about 26 Elo points to Los Angeles."), s)
        self.assertEqual(o["rationale"], schema.expected_rationale_keys(wrap(g, {"official": o, "availability": {}})))
        entry = wrap(g, {"official": o, "availability": {"starter_home": True, "starter_away": True, "market": True, "csv_lines": True}})
        entry["frozen_snapshot"] = snap["id"]
        self.assertEqual(validate_games([entry], params), [])
        # a legacy official without the block (pre-D18 build) gets one rebuilt from the context
        legacy = {k: v for k, v in live.items() if k != "injuries"}
        o2 = predict.freeze_official(legacy, snap, g, pg, params, ctx)
        self.assertEqual(o2["injuries"], live["injuries"])

    def test_frozen_pick_carries_the_snapshot_term_so_p_model_matches_d_total(self):
        """CONTRACT 2.6a: a frozen card's p_home_model is win_prob(factors.d_total) whatever the snapshot
        carried — the latest report's term (same as live), a different one, or none (pre-D18 snapshot)."""
        params = enabled_params()
        g, pg, mk, ctx, _ = scenario_injuries()
        live = predict.predict_game(g, pg, mk, params, ctx)["official"]
        latest = live["injuries"]["elo"]
        self.assertEqual(latest, -26.0)

        def snap(term, p_home, **extra):
            s = {"id": g["game_id"] + "@20261017T140000Z-abc1234", "game_id": g["game_id"], "kickoff_utc": g["kickoff_utc"],
                 "build_at": "2026-10-17T14:00:00Z", "pick": "DEN", "p_model": p_home, "p_market": 0.57, "p_blend": 0.6,
                 "tier": "Lean", "pred_home": 24, "pred_away": 21, "best_price": -125, "best_book": "fanduel", "ev": -0.03,
                 "market_source": "odds_api", "total_used": 44.0, "total_source": "odds_api"}
            if term is not None:
                s["injury_elo"] = term
            s.update(extra)
            return s

        def check(o, expect_d):
            self.assertAlmostEqual(o["factors"]["d_total"], expect_d)
            self.assertAlmostEqual(o["p_home_model"], elo.win_prob(o["factors"]["d_total"]), places=12)
            self.assertAlmostEqual(o["model_spread"], o["factors"]["d_total"] / 25.0)
            self.assertEqual(o["injuries"], live["injuries"])  # the block shown is always the latest report
            entry = wrap(g, {"official": o, "availability": {"starter_home": True, "starter_away": True, "market": True, "csv_lines": True}})
            entry["frozen_snapshot"] = o.get("_snap_id")
            self.assertEqual(validate_games([entry], params), [])
            return o["rationale_sentences"][o["rationale"].index("injuries")]

        # (a) snapshotted after D18 with the same report: identical to the live card's wording
        o = predict.freeze_official(live, snap(latest, live["p_home_model"]), g, pg, params, ctx)
        s = check(o, pg["d"] + latest)
        self.assertEqual(s, live["rationale_sentences"][live["rationale"].index("injuries")])
        self.assertIn("worth about 26 Elo points to Los Angeles", s)
        # (b) a pre-D18 snapshot (no injury_elo key): no term in the number, and the card says so
        o = predict.freeze_official(live, snap(None, elo.win_prob(pg["d"])), g, pg, params, ctx)
        s = check(o, pg["d"])
        self.assertTrue(s.endswith("; not in the frozen pick, which carried no injury term."), s)
        self.assertNotIn("worth about", s)
        # (c) the report changed after the snapshot: what was carried, then what the latest report reads
        o = predict.freeze_official(live, snap(-10.0, elo.win_prob(pg["d"] - 10.0)), g, pg, params, ctx)
        s = check(o, pg["d"] - 10.0)
        self.assertTrue(s.endswith("; the frozen pick carried about 10 Elo points to Los Angeles, or 0.4 points on the spread; "
                                   "the latest report reads about 26 Elo points to Los Angeles."), s)
        # (d) the term carried in the number while the build has since switched it off
        off = predict.predict_game(g, pg, mk, PARAMS, ctx)["official"]
        o = predict.freeze_official(off, snap(latest, elo.win_prob(pg["d"] + latest)), g, pg, PARAMS, ctx)
        self.assertAlmostEqual(o["factors"]["d_total"], pg["d"] + latest)
        self.assertAlmostEqual(o["p_home_model"], elo.win_prob(o["factors"]["d_total"]), places=12)
        s = o["rationale_sentences"][o["rationale"].index("injuries")]
        self.assertTrue(s.endswith("; the frozen pick carried about 26 Elo points to Los Angeles, or 1 point on the spread; "
                                   "the term is switched off in this build."), s)
        # (e) a live (unfrozen) sentence while the term is off is unchanged: no spread clause, no questionable count
        s = off["rationale_sentences"][off["rationale"].index("injuries")]
        self.assertTrue(s.endswith("; shown for context, not in the number yet."), s)
        self.assertNotIn("questionable", s)

    def test_injury_sentence_edge_cases_cancel_and_questionable_only_sides(self):
        params = enabled_params()
        g, pg, mk, ctx, _ = scenario_injuries()
        # a Questionable-only quiet side is named once the term is in the number (it moves it)
        c = with_injuries(ctx, g, make_report(player("A", "WR", "Out")), make_report(player("B", "RB", "Questionable")))
        o = predict.predict_game(g, pg, mk, params, c)["official"]
        s = o["rationale_sentences"][o["rationale"].index("injuries")]
        self.assertEqual(o["injuries"]["elo"], -(7.0) + 4 * 0.25)  # WR 7 out at home; RB 4 * 0.25 away
        self.assertEqual(s, "Denver lists 1 starter out (WR); Los Angeles lists 1 starter questionable (RB); "
                            "worth about 6 Elo points to Los Angeles, or 0.2 points on the spread.")  # 6 / 25 = 0.24 -> one decimal
        # Questionable alone still never triggers the sentence (D18.4), whatever the term
        c = with_injuries(ctx, g, make_report(player("A", "WR", "Questionable")), make_report())
        o = predict.predict_game(g, pg, mk, params, c)["official"]
        self.assertNotIn("injuries", o["rationale"])
        self.assertNotEqual(o["injuries"]["elo"], 0.0)


if __name__ == "__main__":
    unittest.main()
