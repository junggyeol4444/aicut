"""The Premiere plugin's arithmetic, tested without Premiere.

Premiere Pro is not installed in the environment this was written in, so the
plugin is split the same way the Resolve one is: every decision lives in
`plugin/premiere/aicut_plan.js` and is tested here through node, and only the
API calls live in the `.jsx` that needs the application.

These are the parts that would be wrong silently - a frame lost on every cut,
a plan sorted back into source order, or ticks off by a factor nobody spots
until the sequence plays.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from aicut.models import Cut, Episode
from aicut.render.editplan import EditPlan

PLUGIN = Path(__file__).resolve().parent.parent / "plugin" / "premiere"
MODULE = PLUGIN / "aicut_plan.js"
NODE = shutil.which("node")

#: Premiere's own tick rate. Written out rather than read from the module, so a
#: typo in the module is a failure here instead of agreeing with itself.
TICKS_PER_SECOND = 254016000000


def _plan_dict(cuts, **kw):
    episode = Episode(project_id="p", timeline=cuts, **kw)
    plan = EditPlan.from_episode(episode, "/broadcasts/stream.mkv",
                                 render_settings={"fps": 30})
    return json.loads(json.dumps(plan.to_dict()))


@unittest.skipIf(NODE is None, "node is needed to run the plugin's own code")
class NodeTests(unittest.TestCase):
    """Run a snippet against the real module and get its JSON back.

    Reimplementing the logic in Python to test it would only prove the two
    copies agree with each other, which is the failure this is meant to catch.
    """

    def run_js(self, body: str, **names):
        script = (
            "var aicutPlan = require({module});\n"
            "var input = JSON.parse(process.argv[1]);\n"
            "var result;\n"
            "try {{ result = (function () {{ {body} }}()); }}\n"
            "catch (e) {{ result = {{error: e.message || String(e)}}; }}\n"
            "process.stdout.write(JSON.stringify(result));\n"
        ).format(module=json.dumps(str(MODULE)), body=body)
        done = subprocess.run(
            [NODE, "-e", script, json.dumps(names)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    # -- what the plan decided ------------------------------------------------

    def test_it_matches_what_aicut_itself_computes(self):
        """Two implementations of one rule drift; this is the check.

        The plugin re-implements Cut.kept_spans because it runs in Premiere's
        ExtendScript engine and cannot import aicut. Going through a real
        serialised plan is what keeps the copy honest.
        """
        cut = Cut(0, 0.0, 10.0, remove_spans=[[2.0, 3.0], [7.0, 8.5]])
        serialised = _plan_dict([cut])["cuts"][0]
        spans = self.run_js("return aicutPlan.keptSpans(input.cut);", cut=serialised)
        self.assertEqual([tuple(s) for s in spans], cut.kept_spans())

    def test_plan_order_is_kept_not_source_order(self):
        """2.4: a video may open on the moment that happened last."""
        plan = _plan_dict([Cut(0, 100.0, 101.0), Cut(1, 10.0, 11.0)])
        entries = self.run_js("return aicutPlan.clipList(input.plan, 30);", plan=plan)
        self.assertAlmostEqual(entries[0]["inSeconds"], 100.0)
        self.assertAlmostEqual(entries[1]["inSeconds"], 10.0)

    def test_removals_become_separate_clips(self):
        plan = _plan_dict([Cut(0, 0.0, 10.0, remove_spans=[[4.0, 6.0]])])
        entries = self.run_js("return aicutPlan.clipList(input.plan, 30);", plan=plan)
        self.assertEqual(len(entries), 2)

    # -- where they land ------------------------------------------------------

    def test_clips_are_laid_end_to_end_with_no_gap(self):
        """A gap between clips is black frames in the finished video, and the
        plan asked for a cut, not a gap."""
        plan = _plan_dict([Cut(0, 0.0, 2.0), Cut(1, 50.0, 53.0)])
        entries = self.run_js("return aicutPlan.clipList(input.plan, 30);", plan=plan)
        self.assertAlmostEqual(entries[0]["timelineSeconds"], 0.0)
        self.assertAlmostEqual(entries[1]["timelineSeconds"], 2.0)

    def test_the_out_point_makes_the_clip_the_length_the_plan_asked_for(self):
        entries = self.run_js("return aicutPlan.clipList(input.plan, 30);",
                              plan=_plan_dict([Cut(0, 0.0, 1.0)]))
        self.assertEqual(entries[0]["frames"], 30)
        self.assertAlmostEqual(entries[0]["outSeconds"] - entries[0]["inSeconds"], 1.0)

    def test_times_are_snapped_to_the_sequence_grid(self):
        """Inserting at raw seconds leaves sub-frame gaps that accumulate into
        a drift nobody can find by the end of a long timeline."""
        plan = _plan_dict([Cut(0, 1.017, 2.017)])
        entries = self.run_js("return aicutPlan.clipList(input.plan, 30);", plan=plan)
        self.assertAlmostEqual(entries[0]["inSeconds"] * 30, round(entries[0]["inSeconds"] * 30))

    def test_ticks_are_premieres_ticks_not_seconds(self):
        entries = self.run_js("return aicutPlan.clipList(input.plan, 30);",
                              plan=_plan_dict([Cut(0, 2.0, 3.0)]))
        self.assertEqual(entries[0]["inTicks"], str(2 * TICKS_PER_SECOND))

    def test_ticks_are_a_string_because_the_number_will_not_hold_them(self):
        """Six hours in ticks is 5.5e15 - past where a double counts by ones,
        and Premiere's Time.ticks is a string for the same reason."""
        entries = self.run_js("return aicutPlan.clipList(input.plan, 30);",
                              plan=_plan_dict([Cut(0, 21600.0, 21601.0)]))
        self.assertIsInstance(entries[0]["inTicks"], str)
        self.assertEqual(entries[0]["inTicks"], str(21600 * TICKS_PER_SECOND))

    def test_a_non_integer_frame_rate_works(self):
        entries = self.run_js("return aicutPlan.clipList(input.plan, 23.976);",
                              plan=_plan_dict([Cut(0, 0.0, 1.0)]))
        self.assertEqual(entries[0]["frames"], 24)

    # -- refusals -------------------------------------------------------------

    def test_a_span_shorter_than_a_frame_is_dropped_and_reported(self):
        plan = _plan_dict([Cut(0, 0.0, 1.0), Cut(1, 5.0, 5.001)])
        entries = self.run_js("return aicutPlan.clipList(input.plan, 30);", plan=plan)
        dropped = self.run_js("return aicutPlan.droppedSpans(input.plan, 30);", plan=plan)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(dropped), 1)

    def test_a_plan_of_nothing_but_slivers_is_an_error_not_an_empty_sequence(self):
        result = self.run_js("return aicutPlan.clipList(input.plan, 30);",
                             plan=_plan_dict([Cut(0, 5.0, 5.001)]))
        self.assertIn("shorter than one frame", result["error"])

    def test_a_bad_frame_rate_is_refused(self):
        result = self.run_js("return aicutPlan.clipList(input.plan, 0);",
                             plan=_plan_dict([Cut(0, 0.0, 1.0)]))
        self.assertIn("frame rate", result["error"])

    def test_something_that_is_not_a_plan_says_so(self):
        result = self.run_js('return aicutPlan.parse("{\\"hello\\": 1}", "other.json");')
        self.assertIn("not an aicut edit plan", result["error"])

    def test_broken_json_names_the_file(self):
        result = self.run_js('return aicutPlan.parse("{not json", "broken.json");')
        self.assertIn("broken.json", result["error"])

    def test_an_empty_plan_is_refused(self):
        result = self.run_js('return aicutPlan.parse("{\\"cuts\\": []}", "empty.json");')
        self.assertIn("no cuts", result["error"])

    def test_a_real_plan_round_trips(self):
        plan = _plan_dict([Cut(0, 1.0, 2.0)])
        result = self.run_js("return aicutPlan.parse(JSON.stringify(input.plan)).cuts.length;",
                             plan=plan)
        self.assertEqual(result, 1)

    # -- presentation ---------------------------------------------------------

    def test_the_sequence_has_a_name_a_person_can_find(self):
        plan = _plan_dict([Cut(0, 0.0, 1.0)], target_type="shorts")
        name = self.run_js("return aicutPlan.sequenceName(input.plan);", plan=plan)
        self.assertIn("aicut", name)
        self.assertIn("shorts", name)

    def test_the_summary_states_the_length_the_sequence_will_be(self):
        plan = _plan_dict([Cut(0, 0.0, 2.0), Cut(1, 10.0, 13.0)])
        text = self.run_js("return aicutPlan.summary(input.plan, 30);", plan=plan)
        self.assertIn("2 clips", text)
        self.assertIn("5.0s", text)
        self.assertIn("stream.mkv", text)

    def test_the_source_name_survives_either_platforms_separators(self):
        """The plan may have been written on the other kind of machine, and the
        name is what the script matches against the project's bins."""
        names = self.run_js(
            "return [aicutPlan.baseName(input.win), aicutPlan.baseName(input.posix)];",
            win="C:\\\\Users\\\\j\\\\stream.mkv", posix="/broadcasts/stream.mkv")
        self.assertEqual(names, ["stream.mkv", "stream.mkv"])

    def test_the_subtitle_sits_beside_the_plan(self):
        path = self.run_js("return aicutPlan.subtitlePath(input.p);",
                           p="/w/p/plans/ep.json")
        self.assertEqual(path, "/w/p/plans/ep.srt")


class PortabilityTests(unittest.TestCase):
    """ExtendScript is ES3. Anything newer is a syntax error at load, before a
    line of this runs - and there is no console open to say so."""

    BANNED = (
        ("let ", "let"),
        ("const ", "const"),
        ("=>", "arrow functions"),
        ("`", "template literals"),
        ("...", "spread"),
    )

    def test_the_decisions_are_es3(self):
        source = MODULE.read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith(("*", "//", "/*")))
        for needle, what in self.BANNED:
            self.assertNotIn(needle, code, f"aicut_plan.js uses {what}, which ExtendScript has not")

    def test_the_decisions_do_not_need_premiere(self):
        """aicut_plan.js is the half that must run with nothing installed - it
        is what the tests above exercise."""
        code = MODULE.read_text(encoding="utf-8")
        for name in ("app.project", "#include", "new File("):
            self.assertNotIn(name, code, f"aicut_plan.js reaches for {name}")

    def test_the_shell_says_it_was_never_run(self):
        """Not decoration: it is the difference between a plugin that was
        tested and one that was written from documentation."""
        shell = (PLUGIN / "aicut_premiere.jsx").read_text(encoding="utf-8")
        self.assertIn("NOT VERIFIED HERE", shell)


if __name__ == "__main__":
    unittest.main()
