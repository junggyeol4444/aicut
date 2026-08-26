"""Structural consistency, so the drift found by hand cannot come back.

Each of these guards a promise the project makes about itself rather than a
behaviour: a profile that advertises a knob nothing reads, a producer task with
no prompt behind it, a data-model table nothing ever writes, a state that cannot
be reached. Every one of those is a silent lie, and every one was actually
present at some point in this repository.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import tempfile
import unittest

from aicut.config import DEFAULT_PROFILE_PATH, CalibrationProfile
from aicut.db.store import Store
from aicut.llm import prompts
from aicut.llm.base import Producer
from aicut.llm.mock import MockProducer
from aicut.pipeline.states import State, can_transition

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "aicut"
SOURCE = "\n".join(p.read_text(encoding="utf-8") for p in PACKAGE.rglob("*.py"))


def _leaf_keys(node: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in node.items():
        if key.startswith("_"):
            continue
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= _leaf_keys(value, path + ".")
        else:
            keys.add(path)
    return keys


class ProfileConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.profile_data = json.loads(DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
        self.declared = _leaf_keys(self.profile_data)
        self.read = set(re.findall(r'\.get(?:_float|_int)?\(\s*"([a-z_][a-z0-9_.]*)"', SOURCE))

    def _is_read(self, key: str) -> bool:
        # A stage may read a whole group ("render.video") and index it itself.
        return key in self.read or any(key.startswith(f"{prefix}.") for prefix in self.read)

    def test_every_declared_parameter_is_actually_read_somewhere(self):
        """17.1 puts thresholds in the profile; a knob nothing reads is a lie."""
        unread = sorted(key for key in self.declared if not self._is_read(key))
        self.assertEqual(unread, [], f"profile advertises parameters no code reads: {unread}")

    def test_every_provisional_mark_points_at_something_that_exists(self):
        marked = set(self.profile_data["_meta"]["provisional"])
        for path in sorted(marked):
            with self.subTest(path=path):
                self.assertTrue(
                    path in self.declared or any(key.startswith(f"{path}.") for key in self.declared),
                    f"{path} is marked provisional but is not in the profile",
                )

    def test_measured_standards_are_not_marked_as_guesses(self):
        """The loudness targets are published standards, not unmeasured values."""
        profile = CalibrationProfile.load()
        self.assertFalse(profile.is_provisional("render.audio.loudness.integrated_lufs"))
        self.assertTrue(profile.is_provisional("silence.level_db"))

    def test_the_shipped_profile_still_says_it_is_unmeasured(self):
        """17.5: shipping a profile that claims to be measured would be the worst
        possible default."""
        profile = CalibrationProfile.load()
        self.assertIsNone(profile.measured_at)
        self.assertTrue(profile.provisional)


class ProducerConsistencyTests(unittest.TestCase):
    def _methods(self) -> set[str]:
        return {
            name for name in dir(Producer)
            if not name.startswith("_") and name != "complete_json" and callable(getattr(Producer, name))
        }

    def test_every_judgement_has_a_prompt_behind_it(self):
        missing = sorted(self._methods() - set(prompts.task_names()))
        self.assertEqual(missing, [], f"producer tasks with no prompt: {missing}")

    def test_every_judgement_has_an_offline_stand_in(self):
        handlers = {name[len("_task_"):] for name in dir(MockProducer) if name.startswith("_task_")}
        missing = sorted(self._methods() - handlers)
        self.assertEqual(missing, [], f"tasks the mock cannot answer: {missing}")

    def test_no_prompt_is_orphaned(self):
        orphans = sorted(set(prompts.task_names()) - self._methods())
        self.assertEqual(orphans, [], f"prompts nothing calls: {orphans}")

    def test_no_prompt_hardcodes_an_output_shape(self):
        """2장: the forbidden thing is a fixed structure, wherever it is written."""
        banned = ("hook → context", "hook -> context", "always three", "exactly 3 videos")
        for task in prompts.task_names():
            with self.subTest(task=task):
                lowered = prompts.system_for(task).lower()
                for phrase in banned:
                    self.assertNotIn(phrase, lowered)


class SchemaConsistencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(pathlib.Path(self._tmp.name) / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _tables(self) -> set[str]:
        return {
            row[0] for row in self.store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tb_%'"
            )
        }

    def test_every_table_of_the_data_model_exists(self):
        expected = {
            "tb_project", "tb_event", "tb_event_mention", "tb_content_candidate", "tb_episode",
            "tb_edit_timeline", "tb_yt_reference", "tb_source_output_pair", "tb_performance",
            "tb_calibration_profile",
        }
        self.assertTrue(expected <= self._tables(), f"missing: {sorted(expected - self._tables())}")

    def test_every_table_has_something_that_writes_to_it(self):
        """A table nothing ever fills is a data model that describes a different
        program than the one that runs."""
        for table in sorted(self._tables()):
            with self.subTest(table=table):
                self.assertIn(
                    table, SOURCE, f"{table} is declared but no code mentions it",
                )
                self.assertRegex(
                    SOURCE, rf"INSERT(?: OR REPLACE)? INTO {table}\b",
                    f"nothing ever writes to {table}",
                )

    def test_the_episode_table_has_no_time_range(self):
        """13.1: an episode is an ordered set of cuts, not a span of the source."""
        columns = {row[1] for row in self.store.conn.execute("PRAGMA table_info(tb_episode)")}
        self.assertFalse(columns & {"start_sec", "end_sec", "source_start_sec", "source_end_sec"})

    def test_no_write_uses_insert_or_replace_on_a_parent_row(self):
        """REPLACE deletes first, and ON DELETE CASCADE then takes the children -
        this silently destroyed performance rows and queued uploads once."""
        parents = ("tb_episode", "tb_project", "tb_event")
        for table in parents:
            with self.subTest(table=table):
                self.assertNotRegex(SOURCE, rf"INSERT OR REPLACE INTO {table}\b")


class StateMachineConsistencyTests(unittest.TestCase):
    def test_every_state_is_reachable(self):
        reachable = {State.QUEUED}
        frontier = [State.QUEUED]
        while frontier:
            current = frontier.pop()
            for state in State:
                if can_transition(current, state) and state not in reachable:
                    reachable.add(state)
                    frontier.append(state)
        self.assertEqual(set(State) - reachable, set(), "unreachable pipeline states")

    def test_the_gate_is_the_only_door_to_published(self):
        doors = {state for state in State if can_transition(state, State.PUBLISHED)}
        self.assertEqual(doors, {State.REVIEW_PENDING, State.RETRY_QUEUED})

    def test_normal_endings_are_terminal(self):
        for state in (State.PUBLISHED, State.NO_CONTENT):
            with self.subTest(state=state):
                self.assertEqual({s for s in State if can_transition(state, s)}, set())


class DeadCodeTests(unittest.TestCase):
    def test_no_public_helper_is_left_without_a_caller(self):
        """Dead code in a repository this size is a claim about capability that
        nothing backs up."""
        tests_source = "\n".join(
            p.read_text(encoding="utf-8") for p in pathlib.Path(__file__).parent.rglob("*.py")
        )
        page = (PACKAGE / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        everything = SOURCE + tests_source + page

        # Framework entry points are called by name from outside Python we can see.
        exempt = {"main", "do_GET", "do_POST", "emit", "log_message", "setUp", "tearDown",
                  "setUpClass", "tearDownClass", "transcribe", "detect", "complete_json"}
        defined: dict[str, str] = {}
        references: dict[str, int] = {}
        for path in list(PACKAGE.rglob("*.py")) + list(pathlib.Path(__file__).parent.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("__") and not node.name.startswith("test_"):
                        defined.setdefault(node.name, f"{path.name}:{node.lineno}")
                elif isinstance(node, ast.Name):
                    references[node.id] = references.get(node.id, 0) + 1
                elif isinstance(node, ast.Attribute):
                    references[node.attr] = references.get(node.attr, 0) + 1

        dead = sorted(
            f"{name} ({where})" for name, where in defined.items()
            if name not in exempt
            and references.get(name, 0) == 0
            and not name.startswith("_task_")          # dispatched by getattr
            and f'"{name}"' not in everything
        )
        self.assertEqual(dead, [], f"defined but never called: {dead}")


if __name__ == "__main__":
    unittest.main()


class OfflineGuardTests(unittest.TestCase):
    """The README says the suite runs without ffmpeg. This keeps that true.

    A stage that shells out unconditionally breaks it silently: the tests that
    guard media work still skip, but the ones that feed pre-measured signals
    start failing, and only on a machine without ffmpeg - which is nobody's
    machine here. It has already happened once, when tail verification was
    added to parsing.
    """

    def test_parsing_only_touches_the_file_when_it_probed_it(self):
        import inspect

        from aicut.pipeline import parsing

        source = inspect.getsource(parsing.run)
        self.assertIn("inspect_file", source)
        probe_line = source.index("ctx.media = probe(")
        for call in ("verify_tail(", ".validate()"):
            with self.subTest(call=call):
                self.assertIn(call, source)
                self.assertGreater(
                    source.index(call), probe_line,
                    f"{call} runs before the probe guard, so a supplied media object cannot skip it",
                )

    def test_no_stage_shells_out_at_import_time(self):
        """Importing a module must never require ffmpeg to exist."""
        import importlib
        import pkgutil

        import aicut

        for module in pkgutil.walk_packages(aicut.__path__, prefix="aicut."):
            name = module.name
            if name.endswith(("anthropic_provider", "youtube")):
                continue                      # optional third-party imports, guarded at call time
            if name.endswith("__main__"):
                continue                      # importing it runs the CLI, by design
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_the_media_helpers_declare_their_requirement(self):
        """Every ffmpeg-dependent entry point asks for it explicitly, so the
        failure names the missing tool instead of surfacing as FileNotFoundError."""
        import inspect

        from aicut.media import audio, probe, vision

        for module, functions in (
            (audio, ["detect_silences", "rms_envelope", "measure_loudness"]),
            (vision, ["sample_frames", "motion_curve"]),
            (probe, ["probe"]),
        ):
            for name in functions:
                with self.subTest(function=f"{module.__name__}.{name}"):
                    self.assertIn("require_ffmpeg()", inspect.getsource(getattr(module, name)))
