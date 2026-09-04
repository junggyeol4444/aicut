"""Visual measurement (program side).

5.2 requires the passes to watch the screen, not just listen to it, and 1.3
names "no visual awareness" as one of the three failures this project exists to
fix. Two things are measured here:

* **frames** - sampled at the density the profile gives for each pass, handed to
  the reasoning layer as the visual half of a window.
* **motion / scene change** - a cheap per-second number used as a signal (does
  the person move at all during this silence?) and as a boundary *hint* only.
  6.4 is explicit that shot-change detection must never stand in for the first
  pass: nothing changing on screen does not mean nothing is happening.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from aicut.media.ffmpeg_util import require_ffmpeg, run

_SCENE_SCORE = re.compile(r"lavfi\.scene_score=([\d.]+)")
_PTS = re.compile(r"pts_time:([\d.]+)")


@dataclass
class FrameSample:
    at_sec: float
    path: str


@dataclass
class MotionSample:
    at_sec: float
    score: float          # 0..1, ffmpeg scene score between consecutive sampled frames


def sample_frames(
    path: str,
    out_dir: str | Path,
    *,
    start_sec: float,
    duration_sec: float,
    interval_sec: float,
    width: int = 640,
    prefix: str = "f",
) -> list[FrameSample]:
    """Extract one frame every ``interval_sec`` into ``out_dir``."""
    require_ffmpeg()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pattern = str(out / f"{prefix}_%05d.jpg")
    run([
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-ss", f"{start_sec:.3f}", "-t", f"{duration_sec:.3f}", "-i", path,
        "-vf", f"fps=1/{interval_sec},scale={width}:-2",
        "-q:v", "3", pattern,
    ])
    frames = sorted(out.glob(f"{prefix}_*.jpg"))
    return [FrameSample(at_sec=start_sec + i * interval_sec, path=str(p)) for i, p in enumerate(frames)]


def motion_curve(
    path: str,
    *,
    start_sec: float = 0.0,
    duration_sec: float | None = None,
    interval_sec: float = 1.0,
) -> list[MotionSample]:
    """Per-sample visual change score, used for stillness and boundary hints."""
    require_ffmpeg()
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start_sec:.3f}"]
    if duration_sec is not None:
        cmd += ["-t", f"{duration_sec:.3f}"]
    cmd += [
        "-i", path,
        "-vf", f"fps=1/{interval_sec},select='gte(scene,0)',metadata=print:key=lavfi.scene_score:file=-",
        "-f", "null", "-",
    ]
    output = run(cmd)
    return _parse_motion(output, start_sec=start_sec, interval_sec=interval_sec)


def _parse_motion(output: str, *, start_sec: float = 0.0, interval_sec: float = 1.0) -> list[MotionSample]:
    scores = [float(s) for s in _SCENE_SCORE.findall(output)]
    times = [float(t) * 1.0 for t in _PTS.findall(output)]
    samples = []
    for i, score in enumerate(scores):
        at = start_sec + (times[i] if i < len(times) else i * interval_sec)
        samples.append(MotionSample(at_sec=at, score=min(1.0, score)))
    return samples


def stillness(samples: list[MotionSample], start_sec: float, end_sec: float) -> float:
    """Mean visual change across a span; low means the person is not moving.

    Feeds 9.2's "is the person on screen frozen?" signal. The threshold that
    calls a value "still" is a profile parameter, not a constant here.
    """
    inside = [s.score for s in samples if start_sec <= s.at_sec <= end_sec]
    if not inside:
        return 0.0
    return sum(inside) / len(inside)
