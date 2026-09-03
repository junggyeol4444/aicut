"""CLI tests. Every command that does not need network or ffmpeg is exercised."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from aicut.cli import main
from aicut.db.store import Store


def run(*argv: str) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(list(argv))
    return code, buffer.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_doctor_reports_prerequisites_and_the_provisional_warning(self):
        code, out = run("--workspace", str(self.workspace), "doctor")
        self.assertEqual(code, 0)
        self.assertIn("ffmpeg/ffprobe on PATH", out)
        self.assertIn("17.5", out)

    def test_profile_lists_what_is_still_a_guess(self):
        code, out = run("--workspace", str(self.workspace), "profile")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("silence", data["provisional_parameters"])
        self.assertEqual(data["measured_at"], None)

    def test_quota_shows_the_pacific_reset(self):
        code, out = run("--workspace", str(self.workspace), "quota")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["uploads_left_today"], 6)
        self.assertTrue(data["next_reset"].endswith(("-07:00", "-08:00")))

    def test_status_on_an_empty_workspace(self):
        code, out = run("--workspace", str(self.workspace), "status")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_plan_prints_a_readable_plan(self):
        from aicut.models import Cut, Episode
        from aicut.render.editplan import EditPlan

        episode = Episode(
            project_id="p", target_type="long",
            planned_structure={"structure_name": "result_first", "rationale": "open on the win"},
            timeline=[Cut(0, 1800, 1840, scene_role="result"), Cut(1, 30, 70, scene_role="background")],
        )
        path = EditPlan.from_episode(episode, "/src.mkv").save(self.workspace / "plan.json")
        code, out = run("plan", str(path))
        self.assertEqual(code, 0)
        self.assertIn("result_first", out)
        self.assertIn("00:30:00-00:30:40", out)

    def test_learn_pairs_runs_loop_b_offline(self):
        """12.3 B: the loop that needs no network, only a source and a finished cut."""
        source = self.workspace / "source.json"
        output = self.workspace / "output.json"
        source.write_text(json.dumps({"segments": [
            {"start": 0, "end": 4, "text": "hello everyone welcome to the stream"},
            {"start": 60, "end": 64, "text": "this boss keeps killing me"},
            {"start": 600, "end": 604, "text": "i finally beat the boss"},
        ]}), encoding="utf-8")
        output.write_text(json.dumps({"segments": [
            {"start": 0, "end": 4, "text": "i finally beat the boss"},
            {"start": 5, "end": 9, "text": "this boss keeps killing me"},
        ]}), encoding="utf-8")

        code, out = run(
            "--workspace", str(self.workspace), "learn", "pairs",
            "--source-transcript", str(source), "--output-transcript", str(output),
        )
        self.assertEqual(code, 0)
        self.assertIn("dropped 1", out)
        self.assertIn("reordered True", out)

        store = Store(self.workspace / "aicut.db")
        self.assertEqual(len(store.source_output_pairs()), 1)
        store.close()
        self.assertTrue((self.workspace / "knowledge.json").exists())

    def test_learn_pairs_without_transcripts_is_refused(self):
        code, _ = run("--workspace", str(self.workspace), "learn", "pairs")
        self.assertEqual(code, 1)

    def test_calibrate_init_measures_from_a_run_and_records_the_profile(self):
        """17.4 step 1, and 13장: the measured profile lands in the database too."""
        from aicut.analysis.tension import TensionCurve
        from aicut.db.store import Store as _Store
        from aicut.models import Project
        from aicut.pipeline.context import SignalBundle

        store = _Store(self.workspace / "aicut.db")
        project = store.create_project(Project(file_path="/fixture/stream.mkv", duration_sec=600))
        signals = SignalBundle(
            tension=TensionCurve(),
            rms=[(float(i), -55.0 + (i % 40)) for i in range(400)],
        )
        signals.save(self.workspace / project.project_id / "signals.json")
        store.close()

        code, out = run("--workspace", str(self.workspace), "calibrate", "--init", "--channel", "mychannel")
        self.assertEqual(code, 0)
        self.assertIn("silence.level_db", out)
        self.assertIn("step 1 of 17.4", out)

        saved = json.loads((self.workspace / "profiles" / "mychannel.json").read_text(encoding="utf-8"))
        self.assertIn("silence", saved["_meta"]["provisional"])
        self.assertIn("silence.level_db", saved["_meta"]["measured"])

        code, listed = run("--workspace", str(self.workspace), "profile", "--list")
        self.assertEqual(code, 0)
        rows = json.loads(listed)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "mychannel")
        self.assertEqual(rows[0]["channel_ref"], "mychannel")

    def test_calibrate_without_a_dataset_or_init_is_refused(self):
        code, _ = run("--workspace", str(self.workspace), "calibrate")
        self.assertEqual(code, 1)

    def test_calibrate_init_without_a_processed_project_is_refused(self):
        code, _ = run("--workspace", str(self.workspace), "calibrate", "--init")
        self.assertEqual(code, 1)

    def test_candidates_screen_on_an_unknown_project(self):
        code, _ = run("--workspace", str(self.workspace), "candidates", "nope")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()


class ReportWarningsAreSpokenTests(unittest.TestCase):
    """report.json is a record, not a report.

    Every one of these already reached the file. 2.6 says a departure from the
    plan is reported; 16장 says a failed render is visible and does not cost the
    plan; a source that lies about its length is worth knowing before the
    operator wonders why a cut came out empty. Pointing at a JSON file is not
    telling anyone.
    """

    def _spoken(self, report: dict) -> str:
        import io
        from contextlib import redirect_stdout

        from aicut.cli import _print_report_warnings

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _print_report_warnings(report)
        return buffer.getvalue()

    def test_a_truncated_source_is_said_out_loud(self):
        out = self._spoken({"source_warnings": ["the file looks truncated at 19.5s"]})
        self.assertIn("truncated", out)

    def test_an_impossible_plan_is_said_out_loud(self):
        out = self._spoken({"implausible_plans": [
            {"episode_id": "e1", "detail": "planned 90000s from a 3600s source"},
        ]})
        self.assertIn("90000s", out)
        self.assertIn("IMPLAUSIBLE", out)

    def test_a_render_failure_names_the_episode_and_the_recovery(self):
        out = self._spoken({"render_failures": [
            {"episode_id": "e2", "error": "ffmpeg said no",
             "note": "the edit plan survives; re-run the render stage alone (16장)"},
        ]})
        self.assertIn("e2", out)
        self.assertIn("ffmpeg said no", out)
        self.assertIn("re-run the render", out)

    def test_a_caption_less_render_is_said_out_loud(self):
        out = self._spoken({"degraded": [
            {"episode_id": "e3", "reason": "no_subtitles_filter",
             "detail": "captions were NOT burned in: this ffmpeg has no subtitles filter"},
        ]})
        self.assertIn("NOT burned in", out)

    def test_a_length_departure_is_said_out_loud(self):
        """2.6: the hint is a hint, and a departure from it is reported.

        Built from what planning actually writes, not from what the printer
        wishes it wrote - the first version of this read a key that does not
        exist, so the test passed while the real path printed a raw dict.
        """
        out = self._spoken({"length_deviations": [
            {"episode_id": "e4", "hint_sec": 600, "planned_sec": 141.2,
             "reason": "the content did not fit the hinted length"},
        ]})
        self.assertIn("did not fit", out)
        self.assertIn("141.2s", out)
        self.assertIn("600s hint", out)
        self.assertNotIn("{", out, "the raw dict was printed instead of a sentence")

    def test_every_reported_section_is_built_from_what_the_pipeline_writes(self):
        """Guard against the shape drifting: each key the printer reads must be
        one the pipeline actually sets."""
        import inspect

        from aicut.pipeline import planning, rendering

        sources = inspect.getsource(planning) + inspect.getsource(rendering)
        for key in ("episode_id", "planned_sec", "hint_sec", "reason", "detail", "error", "note"):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', sources)

    def test_a_clean_run_says_nothing(self):
        self.assertEqual(self._spoken({"episodes": [{"cuts": 3}]}), "")


class FlagPlacementTests(unittest.TestCase):
    """`aicut run film.mkv --producer anthropic` is what people type.

    argparse's answer to a global flag typed after the subcommand is
    "unrecognized arguments", which reads like the option does not exist - and
    the fix for that, a shared parent parser, silently breaks the other order
    unless its defaults are SUPPRESS. Both orders are pinned here because
    either one failing sends a run to the mock producer without saying so.
    """

    def setUp(self):
        from aicut.cli import build_parser

        self.parser = build_parser()

    def test_the_producer_can_be_named_on_either_side_of_the_subcommand(self):
        before = self.parser.parse_args(["--producer", "anthropic", "run", "x.mkv"])
        after = self.parser.parse_args(["run", "x.mkv", "--producer", "anthropic"])
        self.assertEqual(before.producer, "anthropic")
        self.assertEqual(after.producer, "anthropic",
                         "a flag typed after the subcommand was dropped")

    def test_the_default_still_holds_when_neither_side_names_it(self):
        """The subparser must not write its own default over the global one -
        this is the failure that would run on the mock while saying nothing."""
        self.assertEqual(self.parser.parse_args(["run", "x.mkv"]).producer, "mock")

    def test_it_holds_for_every_global_flag(self):
        for flag, value, attribute in (("--workspace", "/w", "workspace"),
                                       ("--profile", "/p.json", "profile")):
            with self.subTest(flag=flag):
                before = self.parser.parse_args([flag, value, "run", "x.mkv"])
                after = self.parser.parse_args(["run", "x.mkv", flag, value])
                self.assertEqual(getattr(before, attribute), value)
                self.assertEqual(getattr(after, attribute), value)
        self.assertTrue(self.parser.parse_args(["run", "x.mkv", "--strict"]).strict)
        self.assertFalse(self.parser.parse_args(["run", "x.mkv"]).strict)

    def test_every_subcommand_takes_them_not_only_run(self):
        for command, extra in (("resume", ["p1"]), ("plan", ["plan.json"]),
                               ("candidates", ["p1"]), ("doctor", [])):
            with self.subTest(command=command):
                parsed = self.parser.parse_args([command] + extra + ["--producer", "anthropic"])
                self.assertEqual(parsed.producer, "anthropic")
