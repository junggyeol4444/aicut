"""Calibration: measuring the numbers instead of asserting them (17장)."""

from aicut.calibration.metrics import (
    ContentDiscoveryScore,
    PacingScore,
    score_content_discovery,
    score_pacing,
)
from aicut.calibration.dataset import ContentSpan, Dataset, SilenceVerdict
from aicut.calibration.harness import ReplayHarness, build_evaluator, prepare_project
from aicut.calibration.sweep import SweepResult, sweep

__all__ = [
    "Dataset",
    "ContentSpan",
    "SilenceVerdict",
    "ReplayHarness",
    "build_evaluator",
    "prepare_project",
    "PacingScore",
    "ContentDiscoveryScore",
    "score_pacing",
    "score_content_discovery",
    "sweep",
    "SweepResult",
]
