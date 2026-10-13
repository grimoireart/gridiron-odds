"""Front-end render tests: src/render.py + src/page_template.html (D13).

Renders tests/fixtures/model_fixture.json into a temp index.html and checks the
embedded JSON is safe and round-trips, the placeholders are gone, and the
template carries the structural/a11y/design requirements of D13.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import unittest

from src import render

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "model_fixture.json")
TEMPLATE = os.path.join(ROOT, "src", "page_template.html")

SCRIPT_RE = re.compile(r'<script id="model" type="application/json">(.*?)</script>', re.S)


def load_fixture() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def embedded_json(html: str) -> str:
    m = SCRIPT_RE.search(html)
    assert m, "model script element not found"
    return m.group(1)


class RenderHelperTests(unittest.TestCase):
    def test_escapes_script_close_and_line_separators(self):
        raw = '{"a":"</script><b>","b":"x\u2028y\u2029z","c":"a<!--<script>b"}'
        out = render.escape_json_for_script(raw)
        self.assertNotIn("<", out)  # every '<' is \\u003c: covers </script> and <!-- alike
        self.assertNotIn(" ", out)
        self.assertNotIn(" ", out)
        self.assertEqual(json.loads(out), json.loads(raw))  # JSON treats \/ as /

    def test_js_string_escapes_angle_brackets(self):
        out = render.escape_js_string('a"b\\c</script><!--\n')
        self.assertNotIn("<", out)
        self.assertEqual(out, 'a\\"b\\\\c\\u003c/script>\\u003c!--\\n')

    def test_odds_url_substituted_before_model_json(self):
        tpl = ('<script id="model" type="application/json">__MODEL_JSON__</script>'
               '<script>window.GRIDIRON_ODDS_URL = "__ODDS_URL__";</script>')
        html = render.render_page(tpl, {"a": "__ODDS_URL__", "b": "__MODEL_JSON__"}, "https://w.test/")
        self.assertEqual(json.loads(embedded_json(html)), {"a": "__ODDS_URL__", "b": "__MODEL_JSON__"})
        self.assertIn('window.GRIDIRON_ODDS_URL = "https://w.test/";', html)
        with self.assertRaises(ValueError):
            render.render_page(tpl + "__MODEL_JSON__", {}, "")

    def test_serialize_keeps_full_precision(self):
        model = {"p": 0.6291199583450603, "n": None, "s": "ünïcode"}
        text = render.serialize_model(model)
        self.assertIn("0.6291199583450603", text)
        self.assertEqual(json.loads(text), model)

    def test_serialize_rejects_nan(self):
        with self.assertRaises(ValueError):
            render.serialize_model({"x": float("nan")})

    def test_render_requires_placeholders(self):
        with self.assertRaises(ValueError):
            render.render_page("<p>no placeholder</p>", {})

    def test_odds_url_is_escaped_js_string(self):
        html = render.render_page(
            '<script id="model" type="application/json">__MODEL_JSON__</script>'
            '<script>window.GRIDIRON_ODDS_URL = "__ODDS_URL__";</script>',
            {"a": 1}, 'https://x.test/?q="</script>')
        self.assertIn('window.GRIDIRON_ODDS_URL = "https://x.test/?q=\\"\\u003c/script>";', html)


class PageRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = load_fixture()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = os.path.join(cls.tmp.name, "index.html")
        cls.html = render.write_index(cls.model, cls.out, TEMPLATE, odds_url="")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_writes_file_atomically(self):
        self.assertTrue(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + ".tmp"))
        with open(self.out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.html)

    def test_placeholders_replaced(self):
        self.assertNotIn(render.MODEL_PLACEHOLDER, self.html)
        self.assertNotIn(render.ODDS_URL_PLACEHOLDER, self.html)
        self.assertIn('window.GRIDIRON_ODDS_URL = "";', self.html)

    def test_embedded_model_round_trips(self):
        self.assertEqual(json.loads(embedded_json(self.html)), self.model)

    def test_hostile_strings_cannot_break_out(self):
        model = copy.deepcopy(self.model)
        g = model["games"][0]["official"]
        g["rationale_sentences"][0] = 'x</script><img src=x onerror=alert(1)>\u2028\u2029"'
        g["rationale_sentences"][1] = "a<!--<script>b"  # script-data double-escape opener
        model["teams"][0]["name"] = "</SCRIPT>"
        model["teams"][1]["name"] = "<!--"
        html = render.render_page(render.read_template(TEMPLATE), model, 'https://o.test/<!--<script>')
        body = embedded_json(html)
        self.assertNotIn("<", body)
        self.assertNotIn(" ", body)
        self.assertEqual(json.loads(body), model)
        # exactly the template's own script tags, none contributed by data or the URL
        template = render.read_template(TEMPLATE)
        self.assertEqual(html.count("</script>"), template.count("</script>"))
        self.assertEqual(html.count("<script"), template.count("<script"))
        self.assertEqual(html.count("<!--"), template.count("<!--"))

    def test_injury_block_renders_from_fixture(self):
        # D18.3: every fixture game carries the 2.6b object and the page builds the block from it.
        for g in self.model["games"]:
            inj = g["official"]["injuries"]
            self.assertEqual(sorted(inj), ["as_of", "away", "elo", "elo_away", "elo_home", "enabled", "home"])
            for side in ("home", "away"):
                self.assertEqual(sorted(inj[side]), ["available", "players", "qb_listed", "report_filed", "starters_doubtful",
                                                     "starters_out", "starters_questionable", "total_listed"])
        self.assertTrue(any(g["official"]["injuries"]["home"]["players"] for g in self.model["games"]))
        self.assertEqual(sorted(self.model["params"]["injury"]), ["enabled", "gate", "status_mult", "weights"])
        script = self.html[self.html.index("<script>\n'use strict'"):]
        self.assertIn("append(card, [injuryBlock(g)]);", script)
        self.assertIn("'data-injuries'", script)
        # the embedded report round-trips byte-for-byte, names included
        self.assertEqual(json.loads(embedded_json(self.html))["games"][0]["official"]["injuries"], self.model["games"][0]["official"]["injuries"])

    def test_hostile_player_name_renders_as_text(self):
        # D18.3: names and injury strings are untrusted; a markup-looking name must never become markup.
        model = copy.deepcopy(self.model)
        g = next(g for g in model["games"] if g["official"]["injuries"]["home"]["players"])
        p = g["official"]["injuries"]["home"]["players"][0]
        p["name"] = 'Evil <img src=x onerror=alert(1)> Guy</script>'
        p["injury"] = "<b>Knee</b><!--"
        p["position"] = "<svg onload=alert(2)>"
        html = render.render_page(render.read_template(TEMPLATE), model, "")
        template = render.read_template(TEMPLATE)
        self.assertNotIn("<img", html)
        self.assertNotIn("<svg onload", html)
        self.assertEqual(html.count("<script"), template.count("<script"))
        self.assertEqual(html.count("<!--"), template.count("<!--"))
        got = json.loads(embedded_json(html))
        gp = next(x for x in got["games"] if x["game_id"] == g["game_id"])["official"]["injuries"]["home"]["players"][0]
        self.assertEqual(gp["name"], p["name"])
        self.assertEqual(gp["injury"], p["injury"])
        # the page builds the block with text nodes only (no innerHTML anywhere, checked below too)
        script = html[html.index("<script>\n'use strict'"):]
        self.assertIn("document.createTextNode", script)
        self.assertNotIn(".innerHTML", script)

    def test_odds_url_from_env(self):
        old = os.environ.get(render.ODDS_URL_ENV)
        os.environ[render.ODDS_URL_ENV] = "https://odds.example/nfl"
        try:
            html = render.write_index(self.model, os.path.join(self.tmp.name, "env.html"), TEMPLATE)
        finally:
            if old is None:
                del os.environ[render.ODDS_URL_ENV]
            else:
                os.environ[render.ODDS_URL_ENV] = old
        self.assertIn('window.GRIDIRON_ODDS_URL = "https://odds.example/nfl";', html)


class TemplateRequirementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = render.read_template(TEMPLATE)

    def test_single_file_shape(self):
        # A standalone GitHub Pages document (D14): standards mode, utf-8 declared early
        # (python http.server sends no charset), a mobile viewport, one <head> and one <body>.
        low = self.t.lower()
        self.assertTrue(low.lstrip().startswith("<!doctype html>"))
        self.assertIn('<html lang="en">', self.t)
        self.assertLess(low.index('<meta charset="utf-8">'), 1024)
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', self.t)
        self.assertLess(low.index('<meta charset="utf-8">'), low.index("<title>gridiron odds</title>"))
        for tag in ("<head>", "</head>", "<body>", "</body>", "</html>"):
            self.assertEqual(low.count(tag), 1, tag)
        self.assertTrue(low.rstrip().endswith("</html>"))
        self.assertIn("fonts.googleapis.com/css2", self.t)
        for fam in ("JetBrains+Mono", "Archivo", "Inter"):
            self.assertIn(fam, self.t)

    def test_eyrie_tokens_copied_verbatim(self):
        with open(os.path.join(ROOT, "docs", "eyrie-tokens.css"), encoding="utf-8") as fh:
            tokens = fh.read()
        # every token declaration line of the source file appears in the page
        decls = [ln.strip() for ln in tokens.splitlines() if ln.strip().startswith("--")]
        self.assertGreater(len(decls), 40)
        for d in decls:
            self.assertIn(d, self.t, d)
        self.assertIn('@media (prefers-color-scheme: dark)', self.t)
        self.assertIn(':root:not([data-theme="light"])', self.t)
        self.assertIn(':root[data-theme="dark"]', self.t)

    def test_tabs_and_accessibility_markup(self):
        self.assertIn('role="tablist"', self.t)
        self.assertEqual(self.t.count('<button role="tab"'), 4)
        self.assertEqual(self.t.count('<section role="tabpanel"'), 4)
        for label in (">Today<", ">Rankings<", ">Track record<", ">How it works<"):
            self.assertIn(label, self.t)
        self.assertIn("aria-expanded", self.t)
        self.assertIn("ArrowRight", self.t)
        self.assertIn("prefers-reduced-motion", self.t)
        self.assertIn("overflow-x: auto", self.t)

    def test_mandated_copy_and_labels(self):
        self.assertIn("Parlays multiply the house edge. Most of these have negative expected value; "
                      "the ranking shows which lose least. Legs are treated as independent.", self.t)
        self.assertIn("Model-estimated EV at this price", self.t)
        self.assertIn("Lines as of build time", self.t)
        self.assertIn("No graded picks yet; the first build after Sunday", self.t)
        self.assertIn("starter not listed", self.t)
        # no betting-edge claim on the page: the amber chip says what it measures
        self.assertNotIn("'Edge '", self.t)
        self.assertIn("model-only score; the pick follows the blend", self.t)
        self.assertIn("nflverse lines", self.t)

    def test_injury_copy_and_markup(self):
        # D18.3 mandated strings and structure: summary line, toggle with aria wiring, status chips as text,
        # the three report states, the QB carve-out, the stale eyebrow, How-it-works and Track record sections.
        for needle in ("'Injuries · '", "no report filed yet", "injury reports unavailable this build",
                       "(covered by the QB term)", "latest report; the pick was frozen earlier",
                       "shown for context, not in the number yet", "'aria-controls': id", "'inj-' + g.game_id",
                       "class: 'tablewrap', id, hidden: true", "class: 'inj-table'", "statusChip(p.status)",
                       "'Starter' : '—'", "The injury term", "By injury load", "tr.by_injury_load",
                       "Not split by injury load yet", "gate.improvement_required", "holdout_brier_with",
                       "Five caveats", "starters Out", "' Doubtful · '", "' Questionable'",
                       # the enabled branch (D18.3: the verdict and the weights) and the frozen-card wording
                       # (CONTRACT 2.6a: the carried term is read back out of factors, never claimed from the report)
                       "Listed injuries are in the number in this build", "'OL ' + w.OL + ', WR/TE ' + w.WR_TE",
                       "holdout_brier_without", "const stale = g.frozen_snapshot != null;",
                       "not in the frozen pick, which carried no injury term", "'the frozen pick carried '",
                       "'in the ' + (stale ? 'frozen ' : '') + 'number: '"):
            self.assertIn(needle, self.t, needle)
        self.assertNotIn("as_of_snapshot", self.t)  # a non-contract key; the stale eyebrow keys on frozen_snapshot alone
        # chips: danger wash for Out, hyp wash for Doubtful, a line for Questionable; never color alone
        self.assertIn(".chip.st-out { background: var(--danger-wash)", self.t)
        self.assertIn(".chip.st-doubtful { background: var(--hyp-wash)", self.t)
        self.assertIn(".chip.st-questionable, .chip.st-probable { background: var(--paper)", self.t)
        self.assertIn("overflow-wrap: anywhere", self.t)
        # no "injury news" claim anywhere in the page copy (D17)
        self.assertNotIn("injury news", self.t)

    def test_no_innerhtml_and_no_hardcoded_numbers(self):
        script = self.t[self.t.index("<script>\n'use strict'"):]
        self.assertNotIn(".innerHTML", script)
        self.assertNotIn("outerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)
        self.assertNotIn("document.write", script)
        self.assertIn("JSON.parse($('model').textContent)", script)
        self.assertLessEqual(script.count("\n"), 900, "JS should stay under ~900 lines")


if __name__ == "__main__":
    unittest.main()
