"""The Resolve plugin's arithmetic, tested without Resolve.

Resolve is not installed in the environment this was written in, so the plugin
is split: every decision it makes lives in `plugin/resolve/aicut_plan.py` and
is tested here, and only the API calls live in the file that needs the
application. These are the parts that would be wrong silently - a frame out on
every cut, or the plan's order quietly sorted back into source order.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "plugin" / "resolve"
sys.path.insert(0, str(PLUGIN))

from aicut_plan import (  # noqa: E402
    PlanError,
    clip_list,
    dropped_spans,
    kept_spans,
    load_plan,
    subtitle_path,
    summary,
    timeline_name,
)

from aicut.models import Cut, Episode  # noqa: E402
from aicut.render.editplan import EditPlan  # noqa: E402


def _plan_dict(cuts, **kw):
    episode = Episode(project_id="p", timeline=cuts, **kw)
    plan = EditPlan.from_episode(episode, "/broadcasts/stream.mkv",
                                 render_settings={"fps": 30})
    return json.loads(json.dumps(plan.to_dict()))


class KeptSpanTests(unittest.TestCase):
    """The plugin must drop what the plan decided to drop (9.3), or the
    timeline plays the dead air aicut removed."""

    def test_a_cut_with_no_removals_is_itself(self):
        self.assertEqual(kept_spans({"source_start_sec": 1.0, "source_end_sec": 4.0}),
                         [(1.0, 4.0)])

    def test_a_removal_in_the_middle_splits_the_cut(self):
        spans = kept_spans({"source_start_sec": 0.0, "source_end_sec": 10.0,
                            "remove_spans": [[4.0, 6.0]]})
        self.assertEqual(spans, [(0.0, 4.0), (6.0, 10.0)])

    def test_it_matches_what_aicut_itself_computes(self):
        """Two implementations of the same rule drift; this is the check.

        The plugin re-implements Cut.kept_spans because it runs inside
        Resolve's interpreter and cannot import aicut. Going through a real
        serialised plan is what keeps the copy honest.
        """
        cut = Cut(0, 0.0, 10.0, remove_spans=[[2.0, 3.0], [7.0, 8.5]])
        serialised = _plan_dict([cut])["cuts"][0]
        self.assertEqual(kept_spans(serialised), cut.kept_spans())


class ClipListTests(unittest.TestCase):
    def test_end_frame_is_the_last_frame_not_one_past_it(self):
        """Resolve's endFrame is inclusive. One off here makes every cut in the
        timeline a frame long or a frame short, which shows up at export."""
        entries = clip_list(_plan_dict([Cut(0, 0.0, 1.0)]), 30)
        self.assertEqual(entries[0]["startFrame"], 0)
        self.assertEqual(entries[0]["endFrame"], 29)

    def test_plan_order_is_kept_not_source_order(self):
        """2.4: a video may open on the moment that happened last."""
        plan = _plan_dict([Cut(0, 100.0, 101.0), Cut(1, 10.0, 11.0)])
        entries = clip_list(plan, 30)
        self.assertEqual(entries[0]["startFrame"], 3000)
        self.assertEqual(entries[1]["startFrame"], 300)

    def test_removals_become_separate_clips(self):
        plan = _plan_dict([Cut(0, 0.0, 10.0, remove_spans=[[4.0, 6.0]])])
        self.assertEqual(len(clip_list(plan, 30)), 2)

    def test_the_media_pool_item_is_attached_when_given(self):
        sentinel = object()
        entries = clip_list(_plan_dict([Cut(0, 0.0, 1.0)]), 30, media_pool_item=sentinel)
        self.assertIs(entries[0]["mediaPoolItem"], sentinel)

    def test_a_span_shorter_than_a_frame_is_dropped_and_reported(self):
        plan = _plan_dict([Cut(0, 0.0, 1.0), Cut(1, 5.0, 5.001)])
        self.assertEqual(len(clip_list(plan, 30)), 1)
        self.assertEqual(len(dropped_spans(plan, 30)), 1)

    def test_a_plan_of_nothing_but_slivers_is_an_error_not_an_empty_timeline(self):
        with self.assertRaises(PlanError):
            clip_list(_plan_dict([Cut(0, 5.0, 5.001)]), 30)

    def test_a_bad_frame_rate_is_refused(self):
        with self.assertRaises(PlanError):
            clip_list(_plan_dict([Cut(0, 0.0, 1.0)]), 0)

    def test_frame_rates_that_are_not_whole_numbers(self):
        entries = clip_list(_plan_dict([Cut(0, 0.0, 1.0)]), 23.976)
        self.assertEqual(entries[0]["startFrame"], 0)
        self.assertEqual(entries[0]["endFrame"], 23)


class LoadingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_missing_file_says_which(self):
        with self.assertRaises(PlanError) as raised:
            load_plan(str(self.dir / "nope.json"))
        self.assertIn("nope.json", str(raised.exception))

    def test_something_that_is_not_a_plan_says_so(self):
        path = self.dir / "other.json"
        path.write_text('{"hello": 1}', encoding="utf-8")
        with self.assertRaises(PlanError) as raised:
            load_plan(str(path))
        self.assertIn("not an aicut edit plan", str(raised.exception))

    def test_broken_json_names_the_file(self):
        path = self.dir / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(PlanError) as raised:
            load_plan(str(path))
        self.assertIn("broken.json", str(raised.exception))

    def test_an_empty_plan_is_refused(self):
        path = self.dir / "empty.json"
        path.write_text('{"cuts": []}', encoding="utf-8")
        with self.assertRaises(PlanError):
            load_plan(str(path))

    def test_a_real_plan_round_trips(self):
        path = self.dir / "plan.json"
        path.write_text(json.dumps(_plan_dict([Cut(0, 1.0, 2.0)])), encoding="utf-8")
        plan = load_plan(str(path))
        self.assertEqual(len(plan["cuts"]), 1)

    def test_subtitles_are_found_beside_the_plan(self):
        path = self.dir / "plan.json"
        path.write_text(json.dumps(_plan_dict([Cut(0, 1.0, 2.0)])), encoding="utf-8")
        self.assertIsNone(subtitle_path(str(path)))
        (self.dir / "plan.srt").write_text("1\n", encoding="utf-8")
        self.assertEqual(subtitle_path(str(path)), str(self.dir / "plan.srt"))


class PortabilityTests(unittest.TestCase):
    """The plugin runs in Resolve's interpreter, not in aicut's virtualenv.

    Both halves of that sentence are load-bearing: it cannot import aicut, and
    it cannot use syntax newer than the oldest Python a supported Resolve
    ships. Neither failure shows up in the tests above, because those import
    the module on this machine, where both are true.
    """

    FILES = ("aicut_plan.py", "aicut_resolve.py")

    def _trees(self):
        import ast

        for name in self.FILES:
            source = (PLUGIN / name).read_text(encoding="utf-8")
            yield name, ast.parse(source, filename=name)

    def test_the_decisions_do_not_import_aicut(self):
        """aicut_plan.py is the half that must run with nothing installed."""
        import ast

        tree = next(t for name, t in self._trees() if name == "aicut_plan.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                self.assertFalse(name.split(".")[0] == "aicut",
                                 "aicut_plan.py imports {}".format(name))

    def test_no_syntax_newer_than_the_interpreter_resolve_ships(self):
        """`from __future__ import annotations` was in here and is 3.7+; on the
        3.6 interpreter older Resolve builds carry it is a SyntaxError at
        import, before any of this code gets a chance to run."""
        import ast

        for name, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                    self.fail("{} has a __future__ import".format(name))
                self.assertNotIsInstance(node, getattr(ast, "NamedExpr", ()),
                                         "{} uses := (3.8+)".format(name))
                if hasattr(ast, "Match"):
                    self.assertNotIsInstance(node, ast.Match,
                                             "{} uses match (3.10+)".format(name))


class PresentationTests(unittest.TestCase):
    def test_the_timeline_has_a_name_a_person_can_find(self):
        plan = _plan_dict([Cut(0, 0.0, 1.0)], target_type="shorts")
        name = timeline_name(plan)
        self.assertIn("aicut", name)
        self.assertIn("shorts", name)

    def test_the_summary_states_the_length_the_timeline_will_be(self):
        plan = _plan_dict([Cut(0, 0.0, 2.0), Cut(1, 10.0, 13.0)])
        text = summary(plan, 30)
        self.assertIn("2 clips", text)
        self.assertIn("5.0s", text)
        self.assertIn("stream.mkv", text)


if __name__ == "__main__":
    unittest.main()
