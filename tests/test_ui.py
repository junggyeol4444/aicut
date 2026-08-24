"""UI tests (15장). Runs the real HTTP server on a free port."""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from aicut.pipeline.context import RunContext, SignalBundle
from aicut.pipeline.states import State
from aicut.ui.server import serve
from tests import fixtures


def _request(url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls._tmp.name)
        cls.httpd, cls.ui = serve(cls.workspace, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.ui.close()
        cls._tmp.cleanup()

    def seeded_project(self):
        """A project already through PLANNING, so the review screens have content."""
        pipeline = self.ui.pipeline()
        project = pipeline.submit("/fixture/stream.mkv")
        self.ui.store.replace_utterances(project.project_id, fixtures.utterances())
        ctx = RunContext(
            project=project, store=self.ui.store, profile=self.ui.profile(),
            producer=pipeline.producer, workspace=self.workspace, media=fixtures.media(),
            signals=SignalBundle(
                tension=fixtures.tension(), motion=fixtures.motion(),
                silences=fixtures.silences(), speaker_reliability=1.0,
            ),
        )
        ctx.signals.save(ctx.signal_cache_path)
        result = pipeline.run(project, context=ctx, render=False)
        self.assertIs(result.final_state, State.PLANNING)
        return project.project_id

    # -- serving -------------------------------------------------------------
    def test_index_page_is_served(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as res:
            self.assertEqual(res.status, 200)
            self.assertIn(b"aicut", res.read())

    def test_static_path_traversal_is_refused(self):
        status, body = _request(f"{self.base}/../../etc/passwd")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_profile_endpoint_declares_unmeasured_parameters(self):
        """17.5 has to be visible in the UI, not only in the report."""
        status, body = _request(f"{self.base}/api/profile")
        self.assertEqual(status, 200)
        self.assertTrue(body["provisional"])
        self.assertIn("17.4", body["warning"])

    # -- 15.2 ----------------------------------------------------------------
    def test_submitting_a_missing_file_is_rejected(self):
        status, body = _request(f"{self.base}/api/projects", {"source": "/nope/missing.mkv"})
        self.assertEqual(status, 400)
        self.assertIn("file not found", body["error"])

    def test_submitting_without_a_source_is_rejected(self):
        status, body = _request(f"{self.base}/api/projects", {})
        self.assertEqual(status, 400)

    def test_a_real_submission_starts_a_job_that_reaches_a_terminal_state(self):
        source = self.workspace / "empty.mkv"
        source.write_bytes(b"not really a video")
        status, body = _request(f"{self.base}/api/projects", {"source": str(source), "render": False})
        self.assertEqual(status, 200)

        deadline = time.time() + 20
        job = {}
        while time.time() < deadline:
            _, job = _request(f"{self.base}/api/jobs/{body['job_id']}")
            if not job["running"]:
                break
            time.sleep(0.2)
        self.assertFalse(job["running"], "job never finished")
        # ffprobe cannot read this file, so FAILED is the honest outcome and the
        # UI must show it rather than hanging.
        self.assertIn(job["state"], {State.FAILED.value, State.NO_CONTENT.value})
        self.assertTrue(job["log"])

    # -- 15.4 ----------------------------------------------------------------
    def test_candidate_screen_shows_decisions_with_reasons(self):
        pid = self.seeded_project()
        status, body = _request(f"{self.base}/api/projects/{pid}/candidates")
        self.assertEqual(status, 200)
        self.assertTrue(body["candidates"])
        for candidate in body["candidates"]:
            self.assertIn(candidate["decision"], {"produce", "combine", "hold", "reject"})
            self.assertTrue(candidate["reason"], "a decision without a reason is not reviewable")

    def test_human_verdict_is_recorded_and_scored(self):
        pid = self.seeded_project()
        _, body = _request(f"{self.base}/api/projects/{pid}/candidates")
        candidate_id = body["candidates"][0]["candidate_id"]
        status, result = _request(
            f"{self.base}/api/projects/{pid}/candidates",
            {"candidate_id": candidate_id, "verdict": "disagree", "note": "안 나감"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["agreement"]["reviewed"], 1)
        self.assertEqual(result["agreement"]["agreement"], 0.0)

    def test_an_invalid_verdict_is_refused(self):
        pid = self.seeded_project()
        _, body = _request(f"{self.base}/api/projects/{pid}/candidates")
        status, _ = _request(
            f"{self.base}/api/projects/{pid}/candidates",
            {"candidate_id": body["candidates"][0]["candidate_id"], "verdict": "maybe"},
        )
        self.assertEqual(status, 400)

    # -- 15.5 ----------------------------------------------------------------
    def test_episode_list_and_readable_plan(self):
        pid = self.seeded_project()
        status, episodes = _request(f"{self.base}/api/projects/{pid}/episodes")
        self.assertEqual(status, 200)
        self.assertTrue(episodes)

        status, plan = _request(f"{self.base}/api/episodes/{episodes[0]['episode_id']}/plan")
        self.assertEqual(status, 200)
        self.assertIn("structure", plan["readable"])
        self.assertTrue(plan["plan"]["cuts"])

    def test_review_requires_a_named_reviewer(self):
        """11.3: the gate records who released the video."""
        pid = self.seeded_project()
        _, episodes = _request(f"{self.base}/api/projects/{pid}/episodes")
        episode_id = episodes[0]["episode_id"]

        status, body = _request(f"{self.base}/api/episodes/{episode_id}/review",
                                {"action": "approve", "reviewer": ""})
        self.assertEqual(status, 400)
        self.assertIn("reviewer", body["error"])

        status, body = _request(f"{self.base}/api/episodes/{episode_id}/review",
                                {"action": "approve", "reviewer": "junggyeol"})
        self.assertEqual(status, 200)
        self.assertEqual(body["review_status"], "approved")

    def test_unknown_ids_return_404(self):
        self.assertEqual(_request(f"{self.base}/api/jobs/nope")[0], 404)
        self.assertEqual(_request(f"{self.base}/api/episodes/nope/plan")[0], 404)
        self.assertEqual(_request(f"{self.base}/api/projects/nope/report")[0], 404)

    def test_report_is_served_after_a_run(self):
        pid = self.seeded_project()
        status, report = _request(f"{self.base}/api/projects/{pid}/report")
        self.assertEqual(status, 200)
        self.assertEqual(report["final_state"], State.PLANNING.value)
        self.assertTrue(report["provisional_parameters_used"])


if __name__ == "__main__":
    unittest.main()
