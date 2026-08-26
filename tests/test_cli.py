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
