"""Parameter sweep (17.4).

The procedure of 17.4, in order: measure a starting point from the material
itself, sweep the parameters, score each combination with 17.3, keep the best as
this channel's profile. A parameter that has been through this stops being
provisional; one that has not keeps saying so (17.5).
"""

from __future__ import annotations

import itertools
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from aicut.config import CalibrationProfile

log = logging.getLogger(__name__)

Evaluator = Callable[[CalibrationProfile], float]


@dataclass
class SweepResult:
    best_params: dict[str, Any]
    best_score: float
    trials: list[dict[str, Any]] = field(default_factory=list)
    profile: CalibrationProfile | None = None

    def save_trials(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.trials, indent=2, ensure_ascii=False), encoding="utf-8")
        return target


def sweep(
    base: CalibrationProfile,
    grid: dict[str, Sequence[Any]],
    evaluate: Evaluator,
    *,
    channel_ref: str = "",
    name: str | None = None,
) -> SweepResult:
    """Score every combination in ``grid`` and return the best profile.

    Args:
        base: the profile to start from.
        grid: dotted parameter path -> values to try.
        evaluate: scores a profile against the 17.2 dataset (use
            :func:`aicut.calibration.metrics.combined_score`).
    """
    keys = list(grid)
    combinations = list(itertools.product(*(grid[k] for k in keys)))
    log.info("sweeping %d parameters over %d combinations", len(keys), len(combinations))

    trials: list[dict[str, Any]] = []
    best_params: dict[str, Any] = {}
    best_score = float("-inf")

    for values in combinations:
        overrides = dict(zip(keys, values))
        candidate = base.with_overrides(overrides, measured=keys)
        try:
            score = evaluate(candidate)
        except Exception as exc:
            log.warning("trial %r failed: %s", overrides, exc)
            continue
        trials.append({"params": overrides, "score": score})
        if score > best_score:
            best_params, best_score = overrides, score

    if not trials:
        raise RuntimeError("every sweep trial failed; nothing was measured")

    from datetime import datetime, timezone

    winner = base.with_overrides(best_params, measured=keys)
    winner.name = name or (f"{channel_ref}-calibrated" if channel_ref else f"{base.name}-calibrated")
    winner.measured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    winner.eval_score = {"combined": best_score}
    winner.notes = (
        f"measured by a {len(trials)}-trial sweep over {', '.join(keys)} against a 17.2 dataset"
    )
    return SweepResult(best_params=best_params, best_score=best_score, trials=trials, profile=winner)


def initial_estimates(silence_levels_db: Iterable[float], *, quantile: float = 0.15) -> dict[str, Any]:
    """17.4 step 1: read a starting silence level off the channel's own audio.

    Rather than adopting somebody else's -40 dB, take the level distribution of
    this broadcast and put the silence line low in it. It is still only a
    starting point for the sweep, not a measurement of what sounds silent.
    """
    levels = sorted(level for level in silence_levels_db if level > -120)
    if not levels:
        return {}
    index = min(len(levels) - 1, max(0, int(quantile * (len(levels) - 1))))
    return {"silence.level_db": round(levels[index], 1)}
