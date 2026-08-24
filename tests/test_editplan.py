import json
import tempfile
import unittest
from pathlib import Path

from aicut.errors import PlanValidationError
from aicut.models import Cut, Episode, PacingMode, SubtitleLine
from aicut.render.editplan import EditPlan, describe, validate
from aicut.render.timeline import Timeline


def sample_plan() -> EditPlan:
    episode = Episode(
        project_id="p",
        planned_structure={"structure_name": "result_first", "rationale": "open on the win"},
        target_type="long",
        timeline=[
            # deliberately out of source order: the result opens the video (2.4)
            Cut(0, 1800.0, 1840.0, scene_role="result", pacing_mode=PacingMode.KEEP),
            Cut(1, 30.0, 70.0, scene_role="background", remove_spans=[[45.0, 50.0]]),
            Cut(2, 2400.0, 2440.0, scene_role="reaction", speaker_tag="GUEST"),
        ],
        subtitles=[SubtitleLine(0.0, 2.0, "i beat the boss")],
    )
    return EditPlan.from_episode(episode, "/fixture/stream.mkv")


class EditPlanTests(unittest.TestCase):
    def test_round_trip_through_json(self):
        plan = sample_plan()
        with tempfile.TemporaryDirectory() as tmp:
            path = plan.save(Path(tmp) / "plan.json")
            reloaded = EditPlan.load(path)
        self.assertEqual([c.source_start_sec for c in reloaded.cuts], [1800.0, 30.0, 2400.0])
        self.assertEqual(reloaded.cuts[1].remove_spans, [[45.0, 50.0]])
        self.assertIs(reloaded.cuts[0].pacing_mode, PacingMode.KEEP)

    def test_non_linear_order_is_preserved_not_sorted(self):
        """2.4: source time is data, output order is a decision."""
        plan = sample_plan()
        data = plan.to_dict()
        self.assertEqual([c["source_start_sec"] for c in data["cuts"]], [1800.0, 30.0, 2400.0])

    def test_validation_rejects_an_unrenderable_plan(self):
        data = sample_plan().to_dict()
        data["cuts"][0]["source_end_sec"] = data["cuts"][0]["source_start_sec"]
        with self.assertRaises(PlanValidationError):
            validate(data)

    def test_validation_rejects_duplicate_sequence_order(self):
        data = sample_plan().to_dict()
        data["cuts"][1]["sequence_order"] = 0
        with self.assertRaises(PlanValidationError):
            validate(data)

    def test_validation_rejects_a_future_schema(self):
        data = sample_plan().to_dict()
        data["schema_version"] = "99"
        with self.assertRaises(PlanValidationError):
            validate(data)

    def test_description_is_readable_by_a_person(self):
        """MVP 5's success test: can a person predict the video from the plan."""
        text = describe(sample_plan())
        self.assertIn("00:30:00-00:30:40", text)     # the result cut, first
        self.assertIn("result_first", text)
        self.assertIn("role=background", text)

    def test_timeline_maps_source_moments_onto_the_output_clock(self):
        timeline = Timeline.from_cuts(sample_plan().cuts)
        self.assertAlmostEqual(timeline.duration, 40 + 35 + 40)
        self.assertAlmostEqual(timeline.to_output(1800.0), 0.0)      # result opens
        self.assertAlmostEqual(timeline.to_output(30.0), 40.0)       # background follows
        self.assertIsNone(timeline.to_output(47.0))                  # inside a removed span


if __name__ == "__main__":
    unittest.main()
