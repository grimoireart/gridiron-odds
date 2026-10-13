"""Tests for src/injuries.py (D18, CONTRACT 2.6b): position groups, starters from the
depth chart (latest snapshot per unit at or before as_of; Special Teams never count),
the per-team report (ordering, counts, QB coverage, pre-2016 Probable, no-report vs
unavailable), the Elo term, and the degrading D12-style fetches."""
from __future__ import annotations

import contextlib
import copy
import csv
import io
import json
import os
import shutil
import tempfile
import time
import unittest

from src import injuries, schema

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
INJ_FIX = os.path.join(FIX, "injuries_fixture.csv")
DC_FIX = os.path.join(FIX, "depth_chart_fixture.csv")
REAL_DC = os.path.join(ROOT, "data", "depth_charts_2026.csv")
REAL_INJ = os.path.join(ROOT, "data", "injuries_2026.csv")

AS_OF_BUILD = "2026-10-10T14:03:12Z"  # the model fixture's built_at
DAL_QB = "00-0033077"  # Dak Prescott
TB_QB = "00-0041251"  # Jalon Daniels, the model fixture's listed TB starter
MAYFIELD = "00-0034855"


def read_fixture(path, columns):
    return injuries._read_rows(path, columns, None)


def load_params():
    with open(os.path.join(FIX, "model_fixture.json"), encoding="utf-8") as fh:
        return json.load(fh)["params"]


def enabled_params():
    p = copy.deepcopy(load_params())
    p["injury"]["enabled"] = True
    p["injury"]["gate"] = {"holdout_brier_with": 0.2100, "holdout_brier_without": 0.2150, "improvement_required": 0.001}
    return p


INJ_ROWS = read_fixture(INJ_FIX, injuries.INJURY_COLUMNS)
DC_ROWS = read_fixture(DC_FIX, injuries.DEPTH_COLUMNS)
STARTERS = injuries.starters_by_team(DC_ROWS, AS_OF_BUILD)


def by_name(report, name):
    return next(p for p in report["players"] if p["name"] == name)


class PositionGroupTests(unittest.TestCase):
    def test_report_positions(self):
        for pos, grp in (("T", "OL"), ("G", "OL"), ("C", "OL"), ("WR", "WR_TE"), ("TE", "WR_TE"), ("RB", "RB"),
                         ("FB", "RB"), ("DE", "DL_EDGE"), ("DT", "DL_EDGE"), ("NT", "DL_EDGE"), ("LB", "LB"),
                         ("CB", "DB"), ("S", "DB"), ("QB", "QB"), ("K", "ST"), ("P", "ST"), ("LS", "ST")):
            self.assertEqual(injuries.pos_group(pos), grp, pos)

    def test_depth_chart_slots(self):
        for pos, grp in (("LT", "OL"), ("RG", "OL"), ("LDE", "DL_EDGE"), ("RDT", "DL_EDGE"), ("WLB", "LB"),
                         ("RILB", "LB"), ("MLB", "LB"), ("LCB", "DB"), ("NB", "DB"), ("FS", "DB"), ("SS", "DB"),
                         ("PK", "ST"), ("KR", "ST"), ("PR", "ST"), ("H", "ST")):
            self.assertEqual(injuries.pos_group(pos), grp, pos)

    def test_unknown_blank_and_case(self):
        self.assertEqual(injuries.pos_group("XYZ"), "ST")
        self.assertEqual(injuries.pos_group(""), "ST")
        self.assertEqual(injuries.pos_group(None), "ST")
        self.assertEqual(injuries.pos_group(" cb "), "DB")
        for g in ("OL", "WR_TE", "RB", "DL_EDGE", "LB", "DB", "QB", "ST"):
            self.assertIn(g, schema.INJURY_GROUPS)

    def test_unit_of(self):
        self.assertEqual(injuries.unit_of("3WR 1TE"), "O")
        self.assertEqual(injuries.unit_of("2WR 2TE"), "O")
        self.assertEqual(injuries.unit_of("Base 3-4 D"), "D")
        self.assertEqual(injuries.unit_of("Base 4-3 D"), "D")
        self.assertEqual(injuries.unit_of("Special Teams"), "ST")
        self.assertEqual(injuries.unit_of(None), "O")


class StartersTests(unittest.TestCase):
    def test_latest_snapshot_per_unit_at_build(self):
        dal = STARTERS["DAL"]
        # offense comes from the 2026-10-09 offense-only snapshot (the latest <= as_of)
        self.assertEqual(dal["00-0041200"], {"name": "Jaydon Blue", "pos_abb": "RB", "pos_grp": "RB"})
        self.assertNotIn("00-0036997", dal)  # Javonte Williams dropped to rank 2 there
        self.assertEqual(dal["00-0039341"]["pos_abb"], "LT")  # Tyler Guyton
        self.assertNotIn("00-0039500", dal)  # Asim Richards only started in the older snapshot
        # the 2026-10-12 snapshot (after as_of) benched CeeDee Lamb: must be ignored
        self.assertEqual(dal["00-0036358"]["pos_grp"], "WR_TE")
        self.assertEqual(dal["00-0037708"]["pos_abb"], "WR")  # Jalen Tolbert: WR1 slot 3 in that snapshot
        # defense comes from the base snapshot because the partial one carried no defense
        self.assertEqual(dal["00-0038556"], {"name": "DeMarvion Overshown", "pos_abb": "LILB", "pos_grp": "LB"})
        self.assertEqual(dal["00-0035718"]["pos_grp"], "DL_EDGE")  # Quinnen Williams LDE
        self.assertEqual(dal["00-0040865"]["pos_grp"], "DB")  # Caleb Downs (SS and NB) counted once
        self.assertEqual(sum(1 for v in dal.values() if v["pos_abb"] == "QB"), 1)
        self.assertEqual(dal[DAL_QB]["pos_grp"], "QB")

    def test_special_teams_never_start(self):
        dal = STARTERS["DAL"]
        self.assertNotIn("00-0039000", dal)  # Brandon Aubrey PK, rank 1 on Special Teams
        self.assertNotIn("00-0036992", dal)  # KaVontae Turpin KR/PR rank 1, WR rank 3
        for v in dal.values():
            self.assertNotIn(v["pos_abb"], injuries.ST_POSITIONS)
            self.assertNotEqual(v["pos_grp"], "ST")
        for team, m in STARTERS.items():
            for v in m.values():
                self.assertNotEqual(v["pos_grp"], "ST", (team, v))

    def test_as_of_before_the_last_snapshots(self):
        early = injuries.starters_by_team(DC_ROWS, "2026-10-05T00:00:00Z")["DAL"]
        self.assertIn("00-0039500", early)  # Asim Richards was LT1 on 2026-10-01
        self.assertNotIn("00-0039341", early)  # Guyton was rank 2 then
        self.assertNotIn("00-0041200", early)  # Jaydon Blue not yet listed
        self.assertEqual(set(v["pos_grp"] for v in early.values()), {"OL", "QB", "WR_TE", "RB", "LB", "DL_EDGE"})
        self.assertEqual(len(early), 6)  # LT, QB, WR, RB + LILB, LDE (PK excluded, Guyton rank 2)
        # the exact timestamp of a snapshot is included ("at or before")
        exact = injuries.starters_by_team(DC_ROWS, "2026-10-01T06:00:00Z")["DAL"]
        self.assertIn("00-0039500", exact)
        self.assertNotIn("DAL", injuries.starters_by_team(DC_ROWS, "2026-09-30T00:00:00Z"))

    def test_blank_ids_and_rank_2_excluded(self):
        phi = STARTERS["PHI"]
        self.assertTrue(all(k for k in phi))  # the blank-id NT slot is dropped
        self.assertFalse(any(v["pos_abb"] == "NT" for v in phi.values()))
        tb = STARTERS["TB"]
        self.assertEqual(tb[TB_QB]["name"], "Jalon Daniels")
        self.assertNotIn(MAYFIELD, tb)  # rank 2
        for team in ("DAL", "TB", "JAX", "PHI", "CIN", "MIA", "LV", "NE", "DEN", "LAC", "BUF", "LA"):
            self.assertTrue(18 <= len(STARTERS[team]) <= 26, (team, len(STARTERS[team])))
        self.assertNotIn("CHI", STARTERS)

    def test_alias_codes_and_bad_rows(self):
        rows = [
            {"dt": "2026-10-01T00:00:00Z", "team": "LAR", "player_name": "A", "gsis_id": "00-1", "pos_grp": "3WR 1TE", "pos_abb": "WR", "pos_rank": "1"},
            {"dt": "2026-10-01T00:00:00Z", "team": "LAR", "player_name": "B", "gsis_id": "00-2", "pos_grp": "3WR 1TE", "pos_abb": "WR", "pos_rank": "2"},
            {"dt": "", "team": "LA", "player_name": "C", "gsis_id": "00-3", "pos_grp": "3WR 1TE", "pos_abb": "TE", "pos_rank": "1"},
            {"dt": "2026-10-01T00:00:00Z", "team": "XXX", "player_name": "D", "gsis_id": "00-4", "pos_grp": "3WR 1TE", "pos_abb": "TE", "pos_rank": "1"},
        ]
        out = injuries.starters_by_team(rows, "2026-10-02T00:00:00Z")
        self.assertEqual(out, {"LA": {"00-1": {"name": "A", "pos_abb": "WR", "pos_grp": "WR_TE"}}})
        self.assertEqual(injuries.starters_by_team([], "2026-10-02T00:00:00Z"), {})


class ReportTests(unittest.TestCase):
    def test_shape_and_ordering_dal(self):
        r = injuries.report_for(INJ_ROWS, STARTERS, "DAL", 2026, 5, DAL_QB)
        self.assertEqual(set(r), set(schema.INJURY_REPORT_KEYS))
        self.assertTrue(r["available"] and r["report_filed"])
        self.assertEqual([(p["name"], p["status"], p["starter"]) for p in r["players"]], [
            ("CeeDee Lamb", "Out", True),
            ("DeMarvion Overshown", "Out", True),
            ("Tyler Smith", "Questionable", True),
            ("Cobie Durant", "Out", False),
            ("Drew Shelton", "Out", False),
            ("Jonathan Mingo", "Questionable", False),
        ])
        self.assertEqual((r["starters_out"], r["starters_doubtful"], r["starters_questionable"], r["total_listed"]), (2, 0, 1, 6))
        self.assertIsNone(r["qb_listed"])  # Dak is practice-only (blank report_status): not listed
        self.assertFalse(any(p["name"] == "Dak Prescott" for p in r["players"]))
        lamb = by_name(r, "CeeDee Lamb")
        self.assertEqual(lamb, {"gsis_id": "00-0036358", "name": "CeeDee Lamb", "position": "WR", "pos_group": "WR_TE",
                                "status": "Out", "injury": "Ankle", "starter": True, "covered_by_qb_term": False})
        for p in r["players"]:
            self.assertEqual(set(p), set(schema.INJURY_PLAYER_KEYS))
            self.assertIsInstance(p["name"], str)
            self.assertIn(p["status"], schema.INJURY_STATUSES)
            self.assertIn(p["pos_group"], schema.INJURY_GROUPS)
        # the starter's group comes from the depth-chart slot: Tyler Smith is "G" on the report, LG on the chart
        self.assertEqual(by_name(r, "Tyler Smith")["pos_group"], "OL")

    def test_qb_coverage(self):
        r = injuries.report_for(INJ_ROWS, STARTERS, "TB", 2026, 5, TB_QB)
        self.assertEqual(r["qb_listed"], "Questionable")
        daniels = by_name(r, "Jalon Daniels")
        self.assertTrue(daniels["starter"] and daniels["covered_by_qb_term"])
        self.assertEqual(daniels["pos_group"], "QB")
        mayfield = by_name(r, "Baker Mayfield")
        self.assertFalse(mayfield["starter"])
        self.assertTrue(mayfield["covered_by_qb_term"])
        # QBs never count toward the starter tallies
        self.assertEqual((r["starters_out"], r["starters_doubtful"], r["starters_questionable"], r["total_listed"]), (2, 0, 0, 5))
        self.assertEqual([p["name"] for p in r["players"][:3]], ["Antoine Winfield Jr.", "Benjamin Morrison", "Jalon Daniels"])
        # a different listed QB id that is not on the report
        self.assertIsNone(injuries.report_for(INJ_ROWS, STARTERS, "TB", 2026, 5, "00-NOBODY")["qb_listed"])
        self.assertIsNone(injuries.report_for(INJ_ROWS, STARTERS, "TB", 2026, 5, None)["qb_listed"])

    def test_filed_but_nobody_listed_vs_not_filed_vs_unavailable(self):
        kc = injuries.report_for(INJ_ROWS, STARTERS, "KC", 2026, 5, None)
        self.assertTrue(kc["available"] and kc["report_filed"])
        self.assertEqual(kc["players"], [])
        self.assertEqual(kc["total_listed"], 0)
        cle = injuries.report_for(INJ_ROWS, STARTERS, "CLE", 2026, 5, None)
        self.assertTrue(cle["available"])
        self.assertFalse(cle["report_filed"])
        self.assertEqual(cle["players"], [])
        wk6 = injuries.report_for(INJ_ROWS, STARTERS, "DAL", 2026, 6, DAL_QB)
        self.assertFalse(wk6["report_filed"])
        un = injuries.unavailable_report()
        self.assertEqual(un, {"available": False, "report_filed": False, "players": [], "starters_out": 0,
                              "starters_doubtful": 0, "starters_questionable": 0, "total_listed": 0, "qb_listed": None})
        self.assertNotEqual(cle, un)
        self.assertIsNot(injuries.unavailable_report()["players"], un["players"])

    def test_pre_2016_probable(self):
        r = injuries.report_for(INJ_ROWS, STARTERS, "DAL", 2014, 3, "00-0021429")
        self.assertEqual([(p["name"], p["status"]) for p in r["players"]], [
            ("Dez Bryant", "Out"), ("DeMarco Murray", "Questionable"), ("Jason Witten", "Probable"), ("Tony Romo", "Probable"),
        ])
        self.assertTrue(all(not p["starter"] for p in r["players"]))  # no 2014 depth chart
        self.assertEqual((r["starters_out"], r["starters_doubtful"], r["starters_questionable"], r["total_listed"]), (0, 0, 0, 4))
        self.assertEqual(r["qb_listed"], "Probable")

    def test_blank_gsis_player_is_listed_but_never_a_starter(self):
        r = injuries.report_for(INJ_ROWS, STARTERS, "MIA", 2026, 5, None)
        p = by_name(r, "Practice Squad Guard")
        self.assertIsNone(p["gsis_id"])
        self.assertFalse(p["starter"])
        self.assertEqual(p["pos_group"], "OL")
        self.assertEqual(r["players"][-1]["name"], "Storm Duck")  # non-starters sorted Out..Questionable then name

    def test_duplicates_casing_aliases_and_unknown_statuses(self):
        starters = {"LA": {"00-1": {"name": "A One", "pos_abb": "LT", "pos_grp": "OL"}}}
        rows = [
            {"season": "2026", "week": "5", "team": "LAR", "gsis_id": "00-1", "position": "T", "full_name": "A One", "report_status": "questionable", "report_primary_injury": "Knee"},
            {"season": "2026", "week": "5", "team": "LA", "gsis_id": "00-1", "position": "T", "full_name": "A One", "report_status": "OUT", "report_primary_injury": "Knee"},
            {"season": "2026", "week": "5", "team": "LA", "gsis_id": "00-2", "position": "CB", "full_name": "B Two", "report_status": "IR", "report_primary_injury": ""},
            {"season": "2026", "week": "5", "team": "LA", "gsis_id": "00-3", "position": "", "full_name": "", "report_status": "Doubtful", "report_primary_injury": ""},
            {"season": "2026", "week": "6", "team": "LA", "gsis_id": "00-4", "position": "WR", "full_name": "Next Week", "report_status": "Out", "report_primary_injury": "Hip"},
            {"season": "2025", "week": "5", "team": "LA", "gsis_id": "00-5", "position": "WR", "full_name": "Last Year", "report_status": "Out", "report_primary_injury": "Hip"},
        ]
        r = injuries.report_for(rows, starters, "LA", 2026, 5, "00-9")
        self.assertEqual([(p["gsis_id"], p["status"], p["starter"]) for p in r["players"]], [("00-1", "Out", True), ("00-3", "Doubtful", False)])
        self.assertEqual(r["players"][1]["name"], "Unknown")
        self.assertIsNone(r["players"][1]["position"])
        self.assertIsNone(r["players"][1]["injury"])
        self.assertEqual(r["players"][1]["pos_group"], "ST")
        self.assertEqual((r["starters_out"], r["total_listed"]), (1, 2))
        self.assertEqual(injuries.report_for(rows, starters, "STL", 2026, 5, None)["starters_out"], 1)

    def test_rerun_is_byte_identical(self):
        a = injuries.report_for(INJ_ROWS, STARTERS, "NE", 2026, 5, None)
        b = injuries.report_for(list(reversed(INJ_ROWS)), STARTERS, "NE", 2026, 5, None)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual((a["starters_out"], a["starters_questionable"], a["total_listed"]), (3, 6, 13))


class EloTermTests(unittest.TestCase):
    def test_disabled_or_unavailable_is_zero(self):
        params = load_params()
        self.assertFalse(params["injury"]["enabled"])
        dal = injuries.report_for(INJ_ROWS, STARTERS, "DAL", 2026, 5, DAL_QB)
        self.assertEqual(injuries.injury_elo(dal, params), 0.0)
        self.assertEqual(injuries.injury_elo(injuries.unavailable_report(), enabled_params()), 0.0)
        self.assertEqual(injuries.injury_elo({}, enabled_params()), 0.0)
        self.assertEqual(injuries.injury_elo(dal, {}), 0.0)

    def test_formula_counts_starters_only_and_never_the_qb(self):
        params = enabled_params()
        dal = injuries.report_for(INJ_ROWS, STARTERS, "DAL", 2026, 5, DAL_QB)
        # LB Out 4*1 + WR_TE Out 7*1 + OL Questionable 6*0.25; the two Out non-starters add nothing
        self.assertAlmostEqual(injuries.injury_elo(dal, params), -(4 + 7 + 1.5))
        tb = injuries.report_for(INJ_ROWS, STARTERS, "TB", 2026, 5, TB_QB)
        # two DB starters Out; the Questionable listed QB and the Out backup QB are the QB term's business
        self.assertAlmostEqual(injuries.injury_elo(tb, params), -10.0)
        kc = injuries.report_for(INJ_ROWS, STARTERS, "KC", 2026, 5, None)
        self.assertEqual(injuries.injury_elo(kc, params), 0.0)
        probable = injuries.report_for(INJ_ROWS, STARTERS, "DAL", 2014, 3, None)
        self.assertEqual(injuries.injury_elo(probable, params), 0.0)
        params["injury"]["status_mult"]["Questionable"] = 0.0
        self.assertAlmostEqual(injuries.injury_elo(dal, params), -11.0)
        self.assertLessEqual(injuries.injury_elo(dal, params), 0.0)

    def test_injuries_block(self):
        params = enabled_params()
        dal = injuries.report_for(INJ_ROWS, STARTERS, "DAL", 2026, 5, DAL_QB)
        tb = injuries.report_for(INJ_ROWS, STARTERS, "TB", 2026, 5, TB_QB)
        b = injuries.injuries_block(dal, tb, params, AS_OF_BUILD)
        self.assertEqual(set(b), set(schema.INJURIES_KEYS))
        self.assertTrue(b["enabled"])
        self.assertEqual(b["as_of"], AS_OF_BUILD)
        self.assertAlmostEqual(b["elo_home"], -12.5)
        self.assertAlmostEqual(b["elo_away"], -10.0)
        self.assertAlmostEqual(b["elo"], -2.5)
        self.assertIs(b["home"], dal)
        off = injuries.injuries_block(dal, tb, load_params(), AS_OF_BUILD)
        self.assertEqual((off["enabled"], off["elo_home"], off["elo_away"], off["elo"]), (False, 0.0, 0.0, 0.0))

    def test_block_matches_schema_recomputation(self):
        params = enabled_params()
        for team in ("DAL", "TB", "NE", "LAC", "KC"):
            rep = injuries.report_for(INJ_ROWS, STARTERS, team, 2026, 5, None)
            self.assertAlmostEqual(injuries.injury_elo(rep, params), schema._injury_term(rep, params["injury"]))


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridiron-inj-")
        self.err = io.StringIO()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _put_cache(self, name, src):
        shutil.copyfile(src, os.path.join(self.dir, name))

    def _downloader_copy(self, src):
        def dl(url, dest, timeout):
            shutil.copyfile(src, dest)
        return dl

    @staticmethod
    def _downloader_fail(url, dest, timeout):
        raise OSError("boom")

    @staticmethod
    def _downloader_html(url, dest, timeout):
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write("<html>rate limited</html>")

    def _write_rows(self, path, rows, columns):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(columns))
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_offline_uses_cache_or_degrades(self):
        with contextlib.redirect_stderr(self.err):
            self.assertIsNone(injuries.fetch_injuries(2026, self.dir, offline=True))
            self.assertIsNone(injuries.fetch_depth_charts(2026, self.dir, offline=True))
        self.assertIn("injuries_2026", self.err.getvalue())
        self.assertIn("unavailable", self.err.getvalue())
        # a cache that is a mixed-season fixture fails the season check -> degrade
        self._put_cache("injuries_2026.csv", INJ_FIX)
        with contextlib.redirect_stderr(self.err):
            self.assertIsNone(injuries.fetch_injuries(2026, self.dir, offline=True))
        self.assertIn("not season 2026", self.err.getvalue())
        rows2026 = [r for r in INJ_ROWS if r["season"] == "2026"]
        self._write_rows(os.path.join(self.dir, "injuries_2026.csv"), rows2026, injuries.INJURY_COLUMNS)
        self._put_cache("depth_charts_2026.csv", DC_FIX)
        with contextlib.redirect_stderr(self.err):
            inj = injuries.fetch_injuries(2026, self.dir, offline=True)
            dc = injuries.fetch_depth_charts(2026, self.dir, offline=True)
        self.assertEqual(len(inj), len(rows2026))
        self.assertEqual(set(inj[0]), set(injuries.INJURY_COLUMNS))
        self.assertEqual(len(dc), len(DC_ROWS))
        self.assertEqual(set(dc[0]), set(injuries.DEPTH_COLUMNS))
        # the fetched rows drive the same starters as the fixture rows
        self.assertEqual(injuries.starters_by_team(dc, AS_OF_BUILD), STARTERS)

    def test_download_writes_cache_atomically(self):
        src = os.path.join(self.dir, "src.csv")
        self._write_rows(src, DC_ROWS, injuries.DEPTH_COLUMNS)
        with contextlib.redirect_stderr(self.err):
            rows = injuries.fetch_depth_charts(2026, self.dir, offline=False, _downloader=self._downloader_copy(src))
        self.assertEqual(len(rows), len(DC_ROWS))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "depth_charts_2026.csv")))
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])
        self.assertEqual(self.err.getvalue(), "")

    def test_failures_keep_the_cache_and_log(self):
        rows2026 = [r for r in INJ_ROWS if r["season"] == "2026"]
        cache = os.path.join(self.dir, "injuries_2026.csv")
        self._write_rows(cache, rows2026, injuries.INJURY_COLUMNS)
        with open(cache, "rb") as fh:
            before = fh.read()
        with contextlib.redirect_stderr(self.err):
            a = injuries.fetch_injuries(2026, self.dir, offline=False, _downloader=self._downloader_fail)
            b = injuries.fetch_injuries(2026, self.dir, offline=False, _downloader=self._downloader_html)
            c = injuries.fetch_injuries(2026, self.dir, offline=False, _downloader=self._downloader_copy(INJ_FIX))  # wrong seasons inside
        for rows in (a, b, c):
            self.assertEqual(len(rows), len(rows2026))
        with open(cache, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])
        log = self.err.getvalue()
        self.assertIn("download of", log)
        self.assertIn("not a CSV", log)
        self.assertIn("not season 2026", log)
        self.assertEqual(log.count("using the cache"), 3)

    def test_failure_without_cache_is_none(self):
        with contextlib.redirect_stderr(self.err):
            self.assertIsNone(injuries.fetch_injuries(2026, self.dir, offline=False, _downloader=self._downloader_fail))
            self.assertIsNone(injuries.fetch_depth_charts(2026, self.dir, offline=False, _downloader=self._downloader_html))
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual(self.err.getvalue().count("unavailable"), 2)

    def test_shrunken_download_is_rejected(self):
        big = [{"dt": "2026-10-01T00:00:00Z", "team": "KC", "player_name": "P%d" % i, "gsis_id": "00-%05d" % i,
                "pos_grp": "3WR 1TE", "pos_abb": "WR", "pos_rank": "3"} for i in range(1500)]
        cache = os.path.join(self.dir, "depth_charts_2026.csv")
        self._write_rows(cache, big, injuries.DEPTH_COLUMNS)
        small = os.path.join(self.dir, "small.csv")
        self._write_rows(small, big[:100], injuries.DEPTH_COLUMNS)
        with contextlib.redirect_stderr(self.err):
            rows = injuries.fetch_depth_charts(2026, self.dir, offline=False, _downloader=self._downloader_copy(small))
        self.assertEqual(len(rows), 1500)
        self.assertIn("has 100 rows, cache has 1500", self.err.getvalue())
        # a modest shrink (within the slack) is accepted
        ok = os.path.join(self.dir, "ok.csv")
        self._write_rows(ok, big[:600], injuries.DEPTH_COLUMNS)
        with contextlib.redirect_stderr(self.err):
            rows = injuries.fetch_depth_charts(2026, self.dir, offline=False, _downloader=self._downloader_copy(ok))
        self.assertEqual(len(rows), 600)


@unittest.skipUnless(os.path.exists(REAL_DC) and os.path.exists(REAL_INJ), "needs the data/ caches")
class RealDataSmokeTests(unittest.TestCase):
    """Smoke check on the real season files. In GitHub Actions `make test` runs right after `make build`
    against the freshly downloaded files, so only STRUCTURAL assertions run there (a depth-chart quirk on
    a build day must not block the daily publish; D18.1 says a bad feed degrades, never fails). The tight
    bounds (starters per team, exactly one rank-1 QB, the PHI week-5 report, the time budget) are a local
    check of the cached files."""

    IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))

    def test_real_caches_load_fast_and_cover_every_team(self):
        t0 = time.time()
        with contextlib.redirect_stderr(io.StringIO()):
            dc = injuries.fetch_depth_charts(2026, os.path.join(ROOT, "data"), offline=True)
            inj = injuries.fetch_injuries(2026, os.path.join(ROOT, "data"), offline=True)
        if dc is None or inj is None:
            # a degraded CI build (the download failed and nothing was cached): the build already
            # published available=false; there is nothing structural to check here
            self.skipTest("the cached season files are unusable in this checkout")
        starters = injuries.starters_by_team(dc, "2026-10-13T00:00:00Z")
        elapsed = time.time() - t0
        self.assertEqual(set(starters), set(schema.TEAMS))
        for team, m in starters.items():
            self.assertGreaterEqual(len(m), 1, team)
            self.assertFalse(any(v["pos_grp"] == "ST" for v in m.values()), team)
            for v in m.values():
                self.assertEqual(set(v), {"name", "pos_abb", "pos_grp"}, team)
        rep = injuries.report_for(inj, starters, "PHI", 2026, 5, None)
        self.assertEqual(set(rep), set(schema.INJURY_REPORT_KEYS))
        for p in rep["players"]:
            self.assertEqual(set(p), set(schema.INJURY_PLAYER_KEYS))
        if self.IN_ACTIONS:
            return
        # local-only: the tight data-dependent bounds on the cached 2026 files
        self.assertLess(elapsed, 10.0)
        for team, m in starters.items():
            self.assertTrue(18 <= len(m) <= 26, (team, len(m)))
            self.assertEqual(sum(1 for v in m.values() if v["pos_grp"] == "QB"), 1, team)
        self.assertTrue(rep["report_filed"])


if __name__ == "__main__":
    unittest.main()
