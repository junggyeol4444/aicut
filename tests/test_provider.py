"""The reasoning transport (llm/base.py, llm/anthropic_provider.py).

The judgements themselves cannot be unit tested - they are judgements. What can
be tested is everything around them: that a reply is parsed however the model
wrapped it, that a wrong shape is rejected instead of quietly becoming an empty
result, that transient failures are retried, and that the exchange behind a
decision can be reconstructed when a human disagrees with it (15.4).
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from aicut.errors import ProviderError
from aicut.llm.base import Producer, parse_json_block


class ParseTests(unittest.TestCase):
    def test_bare_json(self):
        self.assertEqual(parse_json_block('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(parse_json_block('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json_block('```\n[1, 2]\n```'), [1, 2])

    def test_json_after_a_preamble(self):
        self.assertEqual(
            parse_json_block('Here is what I found:\n{"decision": "produce"}\nHope that helps.'),
            {"decision": "produce"},
        )

    def test_array_after_a_preamble(self):
        self.assertEqual(parse_json_block('sure:\n[{"x": 1}]'), [{"x": 1}])

    def test_a_reply_with_no_json_is_an_error_not_an_empty_result(self):
        with self.assertRaises(ProviderError):
            parse_json_block("I would rather not answer that.")


class ShapeTests(unittest.TestCase):
    """A task that must return a list must not silently accept an object."""

    def _producer(self, payload):
        class Fixed(Producer):
            name = "fixed"

            def complete_json(self, task, system, payload_in):
                return payload

        return Fixed()

    def test_an_object_where_an_array_was_required_is_rejected(self):
        with self.assertRaises(ProviderError):
            self._producer({"not": "a list"}).discover_candidates({})

    def test_a_wrapped_array_is_accepted(self):
        self.assertEqual(self._producer({"items": [{"a": 1}]}).discover_candidates({}), [{"a": 1}])
        self.assertEqual(self._producer({"candidates": [{"b": 2}]}).evaluate_candidates({}), [{"b": 2}])

    def test_an_array_where_an_object_was_required_is_rejected(self):
        with self.assertRaises(ProviderError):
            self._producer([1, 2, 3]).plan_structure({})

    def test_every_task_has_a_system_prompt(self):
        from aicut.llm import prompts

        for task in prompts.task_names():
            with self.subTest(task=task):
                system = prompts.system_for(task)
                self.assertIn("JSON only", system)
                self.assertIn("Never assume a fixed video structure", system)


class _FakeAnthropicModule(types.ModuleType):
    """Minimal stand-in for the anthropic package."""

    def __init__(self, replies, failures=0):
        super().__init__("anthropic")
        self.replies = list(replies)
        self.failures = failures
        self.calls: list[dict] = []
        module = self

        class _Block:
            def __init__(self, text):
                self.type = "text"
                self.text = text

        class _Response:
            def __init__(self, text):
                self.content = [_Block(text)]

        class _Messages:
            def create(self, **kwargs):
                module.calls.append(kwargs)
                if module.failures > 0:
                    module.failures -= 1
                    raise RuntimeError("overloaded_error")
                return _Response(module.replies.pop(0))

        class Anthropic:
            def __init__(self, api_key=None):
                self.api_key = api_key
                self.messages = _Messages()

        self.Anthropic = Anthropic


class AnthropicProducerTests(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("anthropic")
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self._saved
        self._tmp.cleanup()

    def _build(self, replies, *, failures=0, **kwargs):
        from aicut.llm.anthropic_provider import AnthropicProducer

        sys.modules["anthropic"] = _FakeAnthropicModule(replies, failures)
        return AnthropicProducer(api_key="test-key", max_retries=3, **kwargs), sys.modules["anthropic"]

    def test_a_reply_is_parsed_and_the_task_prompt_is_sent(self):
        producer, fake = self._build(['{"structure_name": "result_first", "beats": []}'])
        answer = producer.plan_structure({"content": {"core_summary": "the boss fight"}})

        self.assertEqual(answer["structure_name"], "result_first")
        sent = fake.calls[0]
        self.assertIn("Never assume a number of videos", sent["system"])
        self.assertIn("the boss fight", sent["messages"][0]["content"])

    def test_transient_failures_are_retried(self):
        producer, fake = self._build(['{"ok": true}'], failures=2)
        producer.__dict__["max_retries"] = 3
        import aicut.llm.anthropic_provider as module

        slept = []
        original_sleep = module.time.sleep
        module.time.sleep = slept.append
        try:
            self.assertEqual(producer.plan_structure({}), {"ok": True})
        finally:
            module.time.sleep = original_sleep
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(slept, [2, 4], "retries must back off rather than hammer the API")

    def test_giving_up_raises_rather_than_returning_nothing(self):
        producer, _ = self._build(["never used"], failures=99)
        import aicut.llm.anthropic_provider as module

        original_sleep = module.time.sleep
        module.time.sleep = lambda _s: None
        try:
            with self.assertRaises(ProviderError):
                producer.plan_structure({})
        finally:
            module.time.sleep = original_sleep

    def test_the_exchange_can_be_kept_for_audit(self):
        """15.4: a judgement a reviewer disputes is only checkable if the
        payload that produced it was kept."""
        transcripts = self.dir / "transcripts"
        producer, _ = self._build(['{"titles": ["a", "b", "c"]}'], transcript_dir=transcripts)
        producer.package_metadata({"core_summary": "the tournament"})

        files = list(transcripts.glob("*_package_metadata.json"))
        self.assertEqual(len(files), 1)
        logged = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertIn("the tournament", logged["request"])
        self.assertIn("titles", logged["reply"])

    def test_a_missing_api_key_is_reported_clearly(self):
        import os

        from aicut.llm.anthropic_provider import AnthropicProducer

        sys.modules["anthropic"] = _FakeAnthropicModule([])
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(ProviderError) as raised:
                AnthropicProducer()
            self.assertIn("ANTHROPIC_API_KEY", str(raised.exception))
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved

    def test_an_unknown_producer_name_is_refused(self):
        from aicut.llm import get_producer

        with self.assertRaises(ProviderError):
            get_producer("gpt-whatever")


if __name__ == "__main__":
    unittest.main()
