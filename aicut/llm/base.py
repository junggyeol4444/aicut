"""The reasoning boundary (18장).

Everything the AI is responsible for - what matters, where a content begins and
ends, how many contents exist, whether to produce one at all, which scenes are
needed, their order, the structure, the length, the breath - enters the system
through this one interface. Everything else (decoding, DB, search, rendering,
uploading) is program work and lives outside it.

Two consequences of putting the boundary here:

* A stage never embeds a rule about output shape. It asks a question and gets a
  judgement plus the reasoning behind it, which is what the review UI (15.4) and
  the work report (22장) display.
* The whole pipeline can run against :class:`~aicut.llm.mock.MockProducer`
  offline, so the deterministic half (retrieval, timeline, render, packaging)
  is testable without a model call.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from aicut.errors import ProviderError
from aicut.llm import prompts

log = logging.getLogger(__name__)


class Producer(ABC):
    """A reasoning backend that makes the production judgements of 18장."""

    name = "abstract"

    # -- transport ----------------------------------------------------------
    @abstractmethod
    def complete_json(self, task: str, system: str, payload: dict[str, Any]) -> Any:
        """Answer one task, returning parsed JSON. Implemented per backend."""

    # -- 5장: understanding --------------------------------------------------
    def summarize_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        """First pass over one window: what is going on, and is it worth a second look."""
        return self._object("summarize_window", payload)

    def detail_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Second pass: exact boundaries, beats, reactions inside a marked window."""
        return self._object("detail_window", payload)

    def build_events(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Fold the passes into events with their scattered mentions (5.4)."""
        return self._array("build_events", payload)

    def merge_events(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Unify events that a chunked first pass built more than once (16장)."""
        return self._array("merge_events", payload)

    # -- 6장: discovery / evaluation ----------------------------------------
    def discover_candidates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Which self-contained contents exist in this broadcast? Zero is a valid answer."""
        return self._array("discover_candidates", payload)

    def evaluate_candidates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Decide produce / combine / hold / reject per candidate, with a reason (6.3)."""
        return self._array("evaluate_candidates", payload)

    # -- 7-8장: planning -----------------------------------------------------
    def plan_structure(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Choose this content's structure and issue scene queries for it (7장, 8.1)."""
        return self._object("plan_structure", payload)

    def select_scene(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Pick the best retrieved scene for one beat, or reject them all (8.1)."""
        return self._object("select_scene", payload)

    def judge_pacing(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Comedic beat or dead air? KEEP / TRIM / CUT with a reason (9장)."""
        return self._object("judge_pacing", payload)

    # -- 11장: packaging -----------------------------------------------------
    def package_metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Titles, description, tags, chapters written for this video (11.2)."""
        return self._object("package_metadata", payload)

    # -- 12.3: learning loops ------------------------------------------------
    def analyze_reference(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Loop A: why was this reference video made the way it was (4.4)."""
        return self._object("analyze_reference", payload)

    def compare_source_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Loop B: what a human kept, dropped, reordered, repeated, emphasised (12.3 B)."""
        return self._object("compare_source_output", payload)

    def learn_from_performance(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Loop C: turn viewer response into changes to production strategy (12.2)."""
        return self._object("learn_from_performance", payload)

    # -- helpers -------------------------------------------------------------
    def _object(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.complete_json(task, prompts.system_for(task), payload)
        if not isinstance(result, dict):
            raise ProviderError(f"task {task!r} expected an object, got {type(result).__name__}")
        return result

    def _array(self, task: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        result = self.complete_json(task, prompts.system_for(task), payload)
        if isinstance(result, dict):
            # tolerate {"items": [...]} shaped answers
            for key in ("items", "results", "candidates", "events"):
                if isinstance(result.get(key), list):
                    return result[key]
        if not isinstance(result, list):
            raise ProviderError(f"task {task!r} expected an array, got {type(result).__name__}")
        return result


def parse_json_block(text: str) -> Any:
    """Pull the JSON value out of a model reply that may be fenced or prefaced."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try whichever bracket opens first. Preferring objects would read an array
    # reply that carries a preamble as just its first element.
    candidates = [(text.find(opener), opener, closer) for opener, closer in (("{", "}"), ("[", "]"))]
    for start, opener, closer in sorted(c for c in candidates if c[0] != -1):
        end = text.rfind(closer)
        if end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ProviderError(f"reply contained no parsable JSON: {text[:200]!r}")
