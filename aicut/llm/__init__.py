"""Reasoning providers (18장 boundary)."""

from __future__ import annotations

from typing import Any

from aicut.errors import ProviderError
from aicut.llm.base import Producer

__all__ = ["Producer", "get_producer"]


def get_producer(name: str = "mock", **kwargs: Any) -> Producer:
    """Build a producer by name. ``mock`` runs offline; ``anthropic`` needs a key."""
    if name == "mock":
        from aicut.llm.mock import MockProducer

        return MockProducer()
    if name == "anthropic":
        from aicut.llm.anthropic_provider import AnthropicProducer

        return AnthropicProducer(**kwargs)
    raise ProviderError(f"unknown producer {name!r}; expected 'mock' or 'anthropic'")
