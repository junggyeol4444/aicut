"""Anthropic-backed reasoning provider.

Only the transport lives here; every judgement is defined by the task prompts in
:mod:`aicut.llm.prompts`. Requests are retried on transient failures and the raw
exchange can be logged to disk, because a production judgement that a human
reviewer disagrees with (15.4) is only auditable if the payload that produced it
was kept.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from aicut.errors import ProviderError
from aicut.llm.base import Producer, parse_json_block

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"


class AnthropicProducer(Producer):
    name = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        max_tokens: int = 8000,
        max_retries: int = 3,
        transcript_dir: str | os.PathLike[str] | None = None,
    ):
        try:
            import anthropic  # optional dependency
        except ImportError as exc:  # pragma: no cover - optional dep
            raise ProviderError(
                "the anthropic package is not installed; install aicut[llm] or use --producer mock"
            ) from exc
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=key)
        self._errors = anthropic
        self.model = model
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.transcript_dir = Path(transcript_dir) if transcript_dir else None
        if self.transcript_dir:
            self.transcript_dir.mkdir(parents=True, exist_ok=True)

    def complete_json(self, task: str, system: str, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": body}],
                )
            except Exception as exc:  # transport / rate limit / overload
                last_error = exc
                if attempt == self.max_retries - 1:
                    break
                wait = 2 ** (attempt + 1)
                log.warning("task %s failed (%s); retrying in %ss", task, exc, wait)
                time.sleep(wait)
                continue
            text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
            self._log_exchange(task, body, text)
            return parse_json_block(text)
        raise ProviderError(f"task {task!r} failed after {self.max_retries} attempts: {last_error}")

    def _log_exchange(self, task: str, request: str, reply: str) -> None:
        if not self.transcript_dir:
            return
        path = self.transcript_dir / f"{int(time.time() * 1000)}_{task}.json"
        path.write_text(
            json.dumps({"task": task, "model": self.model, "request": request, "reply": reply}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
