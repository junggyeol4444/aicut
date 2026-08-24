"""Loop A: learning how videos are actually being made (4장, 12.3 A).

Scope is deliberately narrow at the start (4.1): the neighbourhood the channel
actually competes in, not YouTube at large.

The data policy of 4.6 is enforced by construction. This module reads metadata
and public metrics through the API, sends *that* to the analysis step, and stores
only the resulting patterns. It never downloads a reference video, and the
reference table has no column to keep one in. Anything an analysis needs beyond
metadata has to be supplied by the operator for material they are entitled to
analyse, and is discarded after the analysis returns.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from aicut.db.store import Store
from aicut.intelligence.knowledge import ProductionKnowledge, consolidate
from aicut.intelligence.youtube import YouTubeClient
from aicut.llm import Producer

log = logging.getLogger(__name__)

# 4.1: start narrow, widen later if it earns it.
DEFAULT_QUERIES = [
    "게임 스트리머 편집 영상",
    "생방송 하이라이트 편집",
    "합방 하이라이트",
    "스트리머 리액션 모음",
    "게임 방송 다시보기 편집",
]


def collect_references(
    client: YouTubeClient,
    queries: Sequence[str] = DEFAULT_QUERIES,
    *,
    per_query: int = 25,
    **search_params: Any,
) -> list[dict[str, Any]]:
    """Search, then fetch public metrics. Roughly 100 + 1 units per query (11.4)."""
    seen: set[str] = set()
    references: list[dict[str, Any]] = []
    for query in queries:
        try:
            ids = [vid for vid in client.search(query, max_results=per_query, **search_params) if vid not in seen]
        except Exception as exc:
            log.warning("reference search %r failed: %s", query, exc)
            continue
        seen.update(ids)
        for record in client.public_metrics(ids):
            record["found_by"] = query
            references.append(record)
    return references


def analyze(
    producer: Producer,
    store: Store,
    references: Iterable[dict[str, Any]],
    *,
    extra_context: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Ask why each reference was made the way it was, and keep only the answer.

    ``extra_context`` carries anything the operator supplied for material they may
    analyse (their own transcript of a video, notes on its edit). It is passed to
    the analysis and never written to the database - 4.6.
    """
    analyses: list[dict[str, Any]] = []
    for reference in references:
        payload = {
            "video": {
                "title": reference.get("title", ""),
                "description": reference.get("description", "")[:2000],
                "tags": reference.get("tags", []),
                "duration": reference.get("duration", ""),
                "published_at": reference.get("published_at", ""),
            },
            "public_metrics": reference.get("public_metrics", {}),
            "context": (extra_context or {}).get(reference.get("video_id", ""), {}),
            "note": "public metrics only; retention and CTR are unavailable for other channels (4.2)",
        }
        try:
            analysis = producer.analyze_reference(payload)
        except Exception as exc:
            log.warning("analysis failed for %s: %s", reference.get("video_id"), exc)
            continue
        store.save_reference(
            reference.get("video_id", ""),
            reference.get("channel_id", ""),
            reference.get("public_metrics", {}),
            analysis,
        )
        analyses.append(analysis)
    return analyses


def build_knowledge(store: Store) -> ProductionKnowledge:
    """Consolidate every stored reference analysis into production knowledge (4.5)."""
    return consolidate([r["extracted_patterns"] for r in store.references() if r["extracted_patterns"]])
